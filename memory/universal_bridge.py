"""Python bridge between Saturn and the universal-memory Node sidecar.

Saturn keeps its existing memory_manager API. This module starts/connects to the
local sidecar, translates the structured Saturn profile into memory entries, and
exposes the native operations the memory adapters need.

Two authority shapes share this bridge on purpose, so the mapping and the
transport can never drift apart:

``mirror`` (Stage One / hybrid)
    ``save_memory`` pushes a JSON-owned profile into Saturn after the JSON
    write. Failures are reported, never raised - JSON already holds the data.

``authoritative`` (Stage Two / full)
    ``write_profile`` makes Saturn match the profile and *raises*
    :class:`SaturnUnavailable` / :class:`SaturnWriteError` on failure. There is
    no JSON fallback in this path, because falling back would split authority.

Connection defaults stay local and authenticated the same way as before
(``127.0.0.1:8799``, optional ``SATURN_MEMORY_KEY``). Retrieval controls:

* Embeddings run in the engine's lexical mode. The former optional loopback
  Ollama probe is gone: the assistant talks to no process other than its own
  sidecar, and lexical retrieval is a fully supported mode of the engine.
* ``SATURN_MEMORY_WRITE_TIMEOUT`` / ``SATURN_MEMORY_RECALL_TIMEOUT`` bound the
  bridge's HTTP calls.

No external network calls are made: non-loopback endpoints are refused outright.
"""

from __future__ import annotations

from datetime import datetime
import atexit
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import weakref
from types import MappingProxyType
from urllib.parse import quote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SIDECAR_JS = Path(__file__).resolve().parent / "sidecar.js"

# A read that fails on the transport is safe to repeat; a mutation is not.
_READ_RETRIES = 2
# Failed strict writes are queued; older intents past this bound are dropped.
OUTBOX_LIMIT = 25

_BACKEND_DISABLED_VALUES = ("legacy", "json", "off", "0")

# ``sessions`` is the one non-enumerable key in the Saturn profile: a list of
# {date, summary, language} rather than a dict of entries. Native Saturn has no
# "list" concept, so sessions become individual ``summary`` entries whose
# metadata carries the same fields. That keeps one storage model instead of a
# second, half-owned file.
SESSION_CATEGORY = "sessions"


class SaturnError(RuntimeError):
    """Base class for native-backend failures."""


class SaturnUnavailable(SaturnError):
    """The sidecar is not serving, so no durable operation can be guaranteed."""


class SaturnWriteError(SaturnError):
    """The sidecar answered but the authoritative write did not complete."""


class SaturnStaleSnapshot(SaturnWriteError):
    """The snapshot a write was computed from is no longer the current state.

    Raised instead of applying a diff that would erase a write somebody else
    already committed. The caller re-reads and retries on fresh data; silently
    retrying internally would resurrect a stale intent.
    """


class SaturnSnapshotScopeError(SaturnWriteError):
    """The snapshot was read from a different namespace or store than the target.

    Distinct from a stale revision: the revision may match perfectly while the
    snapshot describes somebody else's profile. Writing it would replay one
    namespace's memory over another's, so the bridge refuses before a byte is
    sent and the caller has to re-read from the store it is writing to.
    """


try:
    from core.user_paths import get_user_data_dir
except Exception:  # pragma: no cover - fallback for isolated tests
    def get_user_data_dir() -> Path:
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "SaturnAI"


def _env_value(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return str(default)


def _env_flag(name: str, default: str = "1") -> bool:
    return _env_value(name, default).strip().lower() not in ("0", "false", "no", "off")


def _as_float(name: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(_env_value(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _backend_disabled() -> bool:
    return _env_value("SATURN_MEMORY_BACKEND", "universal").strip().lower() in _BACKEND_DISABLED_VALUES


def _is_local_host(host: str) -> bool:
    """True only for ``localhost`` and literal loopback addresses.

    Prefix matching is not good enough: ``127.evil.example``, ``127.0.0.1.nip.io``
    and ``localhost.example.com`` all start with a loopback-looking string while
    resolving to a remote host. Anything that is not the name ``localhost`` must
    parse as an IP address and be a loopback literal, so a deceptive domain can
    never receive memory traffic.
    """
    candidate = str(host or "").strip().strip("[]").strip().lower()
    if not candidate:
        return False
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _local_url(url: str) -> bool:
    """True when ``url`` is an http(s) URL aimed at a loopback destination."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and _is_local_host(parts.hostname or "")


def _as_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _same_path(left, right) -> bool:
    """True when two store paths name the same file, case-sensitively compared
    only where the filesystem is. Used to detect a sidecar serving a store this
    bridge did not ask for, so it must not treat a case difference as a
    mismatch on Windows."""
    try:
        left_path = os.path.normcase(os.path.abspath(str(left)))
        right_path = os.path.normcase(os.path.abspath(str(right)))
    except Exception:
        return str(left) == str(right)
    return left_path == right_path


def _namespace_id() -> str:
    value = _env_value("SATURN_MEMORY_NAMESPACE", "saturn")
    return value or "saturn"


def _data_path() -> Path:
    override = _env_value("SATURN_MEMORY_FILE")
    if override:
        return Path(override).expanduser().resolve()
    return get_user_data_dir() / "memory" / "universal.jsonl"


# ── Local authentication ─────────────────────────────────────────────────────
#
# The sidecar stores a personal profile, so it is never left open on loopback:
# when no key is configured the bridge mints one, keeps it in the user data
# directory (0600 where the OS supports it) and hands the same value to the
# sidecar it starts. An operator can still supply SATURN_MEMORY_KEY, or point
# SATURN_MEMORY_TOKEN_FILE at a managed secret.

def _token_file() -> Path:
    override = _env_value("SATURN_MEMORY_TOKEN_FILE")
    if override:
        return Path(override).expanduser()
    return get_user_data_dir() / "memory" / "sidecar.token"


def _read_token() -> str:
    try:
        return _token_file().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def _read_or_create_token() -> str:
    """Return the persistent local token, creating it on first use.

    A read is preferred over a write so two processes (or a restart) converge on
    the same secret. The token itself is never logged.
    """
    existing = _read_token()
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    path = _token_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Exclusive create: if another process won the race, use its token.
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return _read_token() or token
        try:
            os.write(fd, (token + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        # An unwritable data directory must not disable memory: the token still
        # authenticates this process against the sidecar it is about to start.
        return token
    return token


def _pid_alive(pid: int) -> bool:
    """Windows-safe liveness check.

    ``os.kill(pid, 0)`` is deliberately not used on Windows: Python maps every
    signal other than the two console events to TerminateProcess, so the check
    would kill the process it is inspecting.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == 259  # STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# The sidecar refuses a request body larger than 256 KiB. A migration batch is
# kept comfortably below that so the JSON envelope, the namespace and the
# per-entry stamping all still fit, and so a single oversized entry cannot push
# a full batch over the cap.
IMPORT_BATCH_BYTES = 192 * 1024


def _entry_bytes(entry: dict) -> int:
    """Serialized size of one import entry, as the sidecar will receive it."""
    try:
        return len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 1
    except Exception:
        return len(str(entry).encode("utf-8")) + 1


def _batched_entries(entries: list[dict], limit: int):
    """Split entries into JSON batches whose serialized size stays under ``limit``.

    Size, not count: one long statement and one short one cost very different
    amounts of body, and a fixed entry-per-batch number would either exceed the
    cap on long rows or waste requests on short ones. An entry that is larger
    than the limit on its own is sent alone, so the failure is the sidecar's
    explicit 413 rather than a silently dropped row.
    """
    batch: list[dict] = []
    size = 0
    for entry in entries:
        weight = _entry_bytes(entry)
        if batch and size + weight > limit:
            yield batch
            batch, size = [], 0
        batch.append(entry)
        size += weight
    if batch:
        yield batch


def _node_binary() -> str | None:
    explicit = _env_value("SATURN_NODE")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    for candidate in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _date_at(value: str) -> str:
    """Event time for a profile row: the stored date, never in the future.

    The engine copies this into ``validFrom`` and hides any row whose window
    has not opened yet, so a fixed hour cannot apply to the current day:
    "today at noon" is still ahead of the clock all morning, and the day's
    memories would stay invisible until noon.
    """
    text = str(value or "").strip()
    if len(text) > 10:
        return text
    day = text or datetime.now().strftime("%Y-%m-%d")
    try:
        moment = datetime.fromisoformat(day + "T12:00:00")
    except ValueError:
        return day + "T12:00:00"
    if moment > datetime.now():
        return datetime.now().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
    return day + "T12:00:00"


_CATEGORY_KIND = {
    "identity": "fact",
    "preferences": "preference",
    "projects": "note",
    "relationships": "fact",
    "wishes": "note",
    "notes": "note",
}

_CATEGORY_IMPORTANCE = {
    "identity": 0.9,
    "preferences": 0.75,
    "projects": 0.65,
    "relationships": 0.75,
    "wishes": 0.55,
    "notes": 0.5,
}

_CATEGORY_LABEL = {
    "identity": "Identity",
    "preferences": "Preferences",
    "projects": "Projects",
    "relationships": "Relationships",
    "wishes": "Wishes",
    "notes": "Notes",
}

_BASE_MEMORY = {
    "identity": {},
    "preferences": {},
    "projects": {},
    "relationships": {},
    "wishes": {},
    "notes": {},
}


# ── Profile -> native entry mapping ───────────────────────────────────────────
#
# These are module-level (not methods) so the migration importer can build
# byte-identical payloads without a live bridge. One mapping, two callers.

def profile_payload(category: str, key: str, entry, namespace: str | None = None) -> dict:
    """Translate one structured profile entry into an Saturn ``remember`` body."""
    value = UniversalBridge._entry_value(entry)
    updated = (entry.get("updated", "") if isinstance(entry, dict) else "") or datetime.now().strftime("%Y-%m-%d")
    kind = _CATEGORY_KIND.get(category, "note")
    label = key.replace("_", " ").strip()
    payload = {
        "text": f"{_CATEGORY_LABEL.get(category, category)} {label}: {value}",
        "kind": kind,
        "tags": ["saturn", f"cat:{category}", f"key:{key}"],
        "src": "saturn",
        "importance": _CATEGORY_IMPORTANCE.get(category, 0.5),
        "at": _date_at(updated),
        "dedupe": False,
        "meta": {
            "saturn_category": category,
            "saturn_key": key,
            "saturn_value": value,
            "saturn_updated": updated,
        },
    }
    if namespace:
        # Destination-bound: a payload imported into the wrong namespace would
        # be invisible to the reader that expects it, so the writer names it.
        payload["namespace"] = str(namespace)
    if category in ("identity", "preferences"):
        payload.update({"subject": "user", "predicate": key, "value": value})
    return payload


def session_payload(session, index: int = 0, namespace: str | None = None) -> dict:
    """One session summary as a native ``summary`` entry.

    The identity is content-derived (date + language + summary hash), not
    position-derived, so re-importing the same ``sessions`` list is a no-op and
    popping a session does not re-key the ones that remain.
    """
    data = session if isinstance(session, dict) else {"summary": str(session or "")}
    summary = str(data.get("summary") or "").strip()
    language = str(data.get("language") or "").strip()
    date = str(data.get("date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    digest = hashlib.sha1(f"{date}\x1f{language}\x1f{summary}".encode("utf-8")).hexdigest()[:12]
    key = f"session_{date}_{digest}"
    payload = {
        "text": f"Saturn session {date}: {summary}",
        "kind": "summary",
        "tags": ["saturn", f"cat:{SESSION_CATEGORY}"],
        "src": "saturn",
        "importance": 0.4,
        "at": _date_at(date),
        "dedupe": False,
        "meta": {
            "saturn_category": SESSION_CATEGORY,
            "saturn_key": key,
            "saturn_value": summary,
            "saturn_updated": date,
            "saturn_language": language,
            "saturn_session_index": int(index),
        },
    }
    if namespace:
        payload["namespace"] = str(namespace)
    return payload


# ── Snapshot provenance ──────────────────────────────────────────────────────
#
# A profile object may only become a write if it provably came from one
# successful authoritative read. A ``provenance`` dict carried on the object is
# *not* enough on its own: ``dict(snapshot)``, a JSON round trip, or a
# hand-built ``{"read_ok": True}`` would be indistinguishable from a real read.
# Every snapshot therefore carries a token this module alone can mint, live
# tokens are tracked by identity in a weak set, and :func:`snapshot_state`
# answers "may this exact object be written back?" - so provenance cannot be
# forged, copied into a plain dict, or inherited from a later successful read.

class _SnapshotToken:
    """Proof that one profile object came from one authoritative read."""

    __slots__ = (
        "read_ok", "revision", "namespace", "file", "source", "error",
        "issued_at", "__weakref__",
    )

    def __init__(
        self,
        read_ok: bool,
        revision: str | None,
        namespace: str | None,
        file: str | None,
        source: str,
        error: str | None,
    ) -> None:
        self.read_ok = bool(read_ok)
        self.revision = str(revision) if revision else None
        self.namespace = namespace
        self.file = file
        self.source = source
        self.error = error
        self.issued_at = datetime.now().isoformat(timespec="seconds")

    def state(self) -> dict:
        return {
            "source": self.source,
            "read_ok": self.read_ok,
            "revision": self.revision,
            "namespace": self.namespace,
            "file": self.file,
            "read_at": self.issued_at,
            "error": self.error,
        }


_LIVE_TOKENS: "weakref.WeakSet[_SnapshotToken]" = weakref.WeakSet()


def _mint_token(
    read_ok: bool,
    revision: str | None,
    namespace: str | None,
    file: str | None,
    source: str,
    error: str | None,
) -> _SnapshotToken:
    token = _SnapshotToken(read_ok, revision, namespace, file, source, error)
    _LIVE_TOKENS.add(token)
    return token


# The one object only this module can present, and only from the read path.
# ``_verified_snapshot`` refuses to mint a live token without it, so no public
# call - a constructor, a ``provenance=`` dict, a copied attribute - can
# manufacture write evidence.
_VERIFIED_READ = object()


class ProfileSnapshot(dict):
    """A profile read plus unforgeable evidence of *that* read.

    The object is still a ``dict`` - prompts, the UI and the reconcilers all
    keep treating it as the profile - but its provenance lives in a minted
    token rather than in a writable attribute. A snapshot stays stale even
    after a later read succeeded, which is what makes the compare-and-swap
    meaningful.

    Construction is deliberately powerless. ``ProfileSnapshot(...)`` - with or
    without a ``provenance`` dict, with or without a ``token`` attribute set
    afterwards - never yields write evidence, because a caller can build any
    dict it likes. Only :func:`_verified_snapshot` mints a live token, and only
    the bridge calls it, immediately after its own successful read.
    """

    def __init__(self, *args, token=None, provenance: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if token is not None:
            # A token may travel with a copy, but it must be one this process
            # minted and still holds; anything else is a forgery attempt.
            if not isinstance(token, _SnapshotToken) or token not in _LIVE_TOKENS:
                raise TypeError(
                    "a profile snapshot token can only be copied from a live read "
                    "of this process; build the profile through the bridge instead"
                )
            self._token = token
            self._claim: dict = {}
            return
        self._token = None
        # A claim is kept for diagnostics only. It is never promoted to
        # evidence - not even a perfectly shaped ``{"read_ok": True}``.
        self._claim = dict(provenance or {})

    # The token is deliberately private: callers validate through
    # ``snapshot_state`` / ``provenance`` instead of minting their own.
    @property
    def token(self):
        return self._token

    @property
    def provenance(self) -> MappingProxyType:
        if self._token is None:
            claim = dict(self._claim)
            claim["read_ok"] = False
            claim["issued"] = False
            claim.setdefault("error", "constructed outside a verified bridge read")
            return MappingProxyType(claim)
        return MappingProxyType(self._token.state())

    @property
    def revision(self) -> str | None:
        return self._token.revision if self._token is not None else None

    @property
    def writable(self) -> bool:
        return bool(self._token is not None and self._token.read_ok)

    # A snapshot is evidence, not a value. Copying keeps the evidence; turning
    # it into a plain dict cannot be prevented in Python, which is why the
    # write gate asks for a live token instead of trusting a dict's shape.
    def copy(self) -> "ProfileSnapshot":
        if self._token is None:
            return ProfileSnapshot(dict(self), provenance=self._claim)
        return ProfileSnapshot(dict(self), token=self._token)

    def __copy__(self) -> "ProfileSnapshot":
        return self.copy()

    def __deepcopy__(self, memo) -> "ProfileSnapshot":
        import copy as _copy

        clone = self.copy()
        clone.clear()
        clone.update(_copy.deepcopy(dict(self), memo))
        memo[id(self)] = clone
        return clone


class ProfileOutage(ProfileSnapshot):
    """A profile whose authoritative read *failed*.

    Callers get the same dict shape (so prompt construction and the UI keep
    working), but the object carries failed-read provenance, no revision, and
    a distinct type. Every authoritative write path refuses it - including
    after the store recovers and a later read succeeds - so a transient outage
    can never be saved back as "the user deleted everything".
    """

    def __init__(self, reason: str = "", *, namespace: str | None = None, file=None):
        super().__init__(
            {category: {} for category in _BASE_MEMORY},
            token=_mint_token(
                False,
                None,
                namespace if namespace is not None else _namespace_id(),
                str(file) if file is not None else None,
                "saturn",
                str(reason or "the authoritative store could not be read"),
            ),
        )

    @property
    def reason(self) -> str:
        return self._token.error or "the authoritative store could not be read"

    @property
    def writable(self) -> bool:
        return False


def _verified_snapshot(
    profile: dict,
    *,
    revision: str | None = None,
    namespace: str | None = None,
    file: str | None = None,
    source: str = "saturn",
    proof=None,
) -> ProfileSnapshot:
    """Mint the one object that may be written back: a verified bridge read.

    Private, and guarded by a sentinel that never leaves this module, so the
    only way to obtain writable evidence is for the bridge to call it right
    after ``/export`` answered. Without the proof the object is still readable
    and still a profile, it simply cannot be written back.
    """
    if proof is not _VERIFIED_READ:
        return ProfileSnapshot(profile)
    return ProfileSnapshot(
        profile,
        token=_mint_token(True, revision, namespace, file, source, None),
    )


def _attest_read(
    profile: dict,
    *,
    revision: str | None = None,
    namespace: str | None = None,
    file: str | None = None,
    source: str = "saturn",
) -> ProfileSnapshot:
    """Promote a plain-dict *transport* read into a writable snapshot.

    Private on purpose. The only caller is ``memory_manager._load_native``,
    and only with the object its own ``bridge.load_memory`` returned in the
    same call - an adapter that answers with a plain dict instead of a
    :class:`ProfileSnapshot`. A caller that builds a profile itself has no
    equivalent: the public constructor mints no evidence.
    """
    if isinstance(profile, ProfileSnapshot):
        state = snapshot_state(profile)
        if not state["ok"]:
            return profile
        revision = revision or state["revision"]
        namespace = state["namespace"] if state["namespace"] is not None else namespace
        file = state["file"] if state["file"] is not None else file
    return _verified_snapshot(
        profile,
        revision=revision,
        namespace=namespace,
        file=file,
        source=source,
        proof=_VERIFIED_READ,
    )


def snapshot_state(profile) -> dict:
    """Whether ``profile`` may be written back to the authoritative store.

    Returns ``{"ok", "issued", "writable", "reason", "revision", "namespace",
    "file", "source"}``. ``ok`` is true only for an object carrying a token this
    module minted for a *successful* read, so a plain dict, a forged
    ``provenance`` attribute, and a failed-read snapshot are all refused.
    """
    if isinstance(profile, ProfileOutage):
        return {
            "ok": False,
            "issued": True,
            "writable": False,
            "reason": (
                "this profile came from a failed read of the native store "
                f"({profile.reason})"
            ),
            "revision": None,
            "namespace": profile._token.namespace,
            "file": profile._token.file,
            "source": "saturn",
        }
    token = getattr(profile, "_token", None)
    if not isinstance(token, _SnapshotToken) or token not in _LIVE_TOKENS:
        return {
            "ok": False,
            "issued": False,
            "writable": False,
            "reason": (
                "the profile was not produced by an authoritative read of the "
                "native store"
            ),
            "revision": None,
            "namespace": None,
            "file": None,
            "source": None,
        }
    if not token.read_ok:
        return {
            "ok": False,
            "issued": True,
            "writable": False,
            "reason": (
                "this profile came from a failed read of the native store "
                f"({token.error or 'unknown reason'})"
            ),
            "revision": None,
            "namespace": token.namespace,
            "file": token.file,
            "source": token.source,
        }
    return {
        "ok": True,
        "issued": True,
        "writable": True,
        "reason": "",
        "revision": token.revision,
        "namespace": token.namespace,
        "file": token.file,
        "source": token.source,
    }


class UniversalBridge:
    def __init__(self) -> None:
        self.port = int(_env_value("SATURN_MEMORY_PORT", "8799") or "8799")
        self.host = _env_value("SATURN_MEMORY_HOST", "127.0.0.1") or "127.0.0.1"
        self.base_url = _env_value(
            "SATURN_MEMORY_URL", f"http://{self.host}:{self.port}"
        ).rstrip("/")
        # A key from the environment wins; otherwise adopt the persisted local
        # token (read-only here - it is created on first start). Loading it now
        # means an instance that attaches to a running sidecar is authenticated
        # from its very first probe.
        self.api_key = _env_value("SATURN_MEMORY_KEY") or _read_token()
        self.file = _data_path()
        # The store scope every read, write and delete is confined to. The
        # sidecar is started with the same value (``_sidecar_env``), so a raw
        # ``/export`` can never be mistaken for "everything in the file".
        self.namespace = _namespace_id()
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._available: bool | None = None
        # Ownership: only the process that spawned the sidecar may stop it. A
        # second instance that found a healthy sidecar must never terminate the
        # first instance's store on exit.
        self._owns_process = False
        self._spawn_lock = Path(str(self.file) + ".sidecar.lock")
        self._status: dict = {}
        self._outbox_lock = threading.Lock()
        self.write_timeout = _as_float("SATURN_MEMORY_WRITE_TIMEOUT", 4.0, 0.25, 30.0)
        self.recall_timeout = _as_float("SATURN_MEMORY_RECALL_TIMEOUT", 2.5, 0.25, 30.0)
        # Ingestion is the one call that is allowed to take minutes: a scanned
        # PDF renders every page to an image and runs OCR over each of them at
        # roughly a second a page. The ordinary write timeout would abort it
        # after the first page.
        self.ingest_timeout = _as_float("SATURN_MEMORY_INGEST_TIMEOUT", 180.0, 5.0, 900.0)
        self._last_error = ""
        self._shadow_status = "idle"
        self._warmup_thread: threading.Thread | None = None
        self._start_cooldown_until = 0.0

    # ------------------------------------------------------------------ HTTP
    def _endpoint(self) -> tuple[str, str, int]:
        """Resolve ``base_url`` and refuse anything that is not loopback.

        Validation happens before a single byte is written, so a deceptive host
        (``127.0.0.1.evil.example``) or a redirect target can never receive
        memory content. ``http.client`` is used instead of ``urlopen`` because
        it does not follow redirects at all: a 3xx is reported as a failure
        rather than silently re-posted somewhere else.
        """
        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https"):
            raise SaturnUnavailable(f"Saturn memory URL is not http(s): {self.base_url}")
        host = parts.hostname or ""
        if not _is_local_host(host):
            raise SaturnUnavailable(
                f"refusing to send memory to a non-loopback endpoint: {host or self.base_url}"
            )
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return parts.scheme, host, port

    def _request(self, method: str, path: str, payload: dict | None = None, timeout: float = 10.0):
        """One HTTP call, with a bounded retry for idempotent reads.

        A GET is retried on a transport failure because the store did not
        change; a mutation never is, because a lost response on a write is
        indistinguishable from a committed write. The compare-and-swap revision
        is what makes a retried write safe, and the outbox is what makes a
        failed one recoverable.
        """
        retries = _READ_RETRIES if method.upper() == "GET" else 0
        delay = 0.08
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return self._request_once(method, path, payload=payload, timeout=timeout)
            except urllib.error.HTTPError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
        assert last_error is not None
        raise last_error

    def _request_once(self, method: str, path: str, payload: dict | None = None, timeout: float = 10.0):
        status, data = self._request_raw(method, path, payload=payload, timeout=timeout)
        if status >= 300:
            raise urllib.error.HTTPError(
                f"{self.base_url}{path}", status, f"HTTP {status}", {}, None
            )
        return data

    def _request_raw(
        self, method: str, path: str, payload: dict | None = None, timeout: float = 10.0
    ) -> tuple[int, dict]:
        """One HTTP exchange, returning ``(status, body)`` and never raising on 4xx/5xx.

        Everything else goes through ``_request``, which treats any non-2xx as a
        failure. Ingestion cannot: the sidecar explains itself in the body
        ("Tesseract not found", "PDF contains no extractable text"), and an
        ``HTTPError`` throws that body away, leaving the user with "HTTP 400".
        """
        scheme, host, port = self._endpoint()
        body = None
        headers = {"accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["content-type"] = "application/json"
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        connection_class = (
            http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_class(host, port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
        finally:
            try:
                connection.close()
            except Exception:
                pass
        return status, (json.loads(raw) if raw else {})

    def health(self, timeout: float = 1.0) -> bool:
        try:
            data = self._request("GET", "/health", timeout=timeout)
            return bool(data.get("ok"))
        except Exception:
            return False

    def status(self, timeout: float = 1.5, namespace: str | None = None) -> dict:
        """Rich sidecar state: mode, revision, count, embeddings, last error."""
        path = "/status"
        if namespace:
            path += "?namespace=" + quote(str(namespace), safe="")
        try:
            data = self._request("GET", path, timeout=timeout)
        except Exception as exc:
            self._last_error = str(exc)
            return {}
        return data if isinstance(data, dict) else {}

    def health_report(self, timeout: float = 1.5) -> dict:
        """One dict a supervisor (or a bug report) can read."""
        data = self.status(timeout=timeout, namespace=self.namespace)
        return {
            "ok": bool(data.get("ok")) or self.health(timeout=timeout),
            "reachable": bool(data),
            "mode": str(data.get("mode") or ""),
            "revision": str(data.get("revision") or ""),
            "namespace": str(data.get("namespace") or self.namespace),
            "file": str(data.get("file") or self.file),
            "count": data.get("count"),
            "semantic": data.get("semantic"),
            "model": data.get("model"),
            "schema_version": data.get("schemaVersion"),
            "compact_bytes": data.get("compactBytes"),
            "uptime_ms": data.get("uptimeMs"),
            "pid": data.get("pid"),
            "sidecar_error": data.get("lastError"),
            "last_error": data.get("lastError") or self._last_error,
            "pending_writes": self.pending_writes(),
            "owned": bool(self._owns_process),
        }

    def available(self, autostart: bool = True) -> bool:
        if not _env_flag("SATURN_MEMORY_ENABLED", "1") or _backend_disabled():
            return False
        if self._available:
            return True
        with self._lock:
            if self._available:
                return True
            if self.health():
                self._available = True
                return True
            if not autostart:
                return False
            if not _env_flag("SATURN_MEMORY_AUTOSTART", "1"):
                return False
            if not self._start():
                return False
            self._available = True
            return True

    def is_ready(self) -> bool:
        """True only when the sidecar is already known to be serving.

        Deliberately does not probe, start Node, or touch the network, so it is
        safe to call from prompt construction, recall, and app startup.
        """
        return (
            bool(self._available)
            and _env_flag("SATURN_MEMORY_ENABLED", "1")
            and not _backend_disabled()
        )

    def warm_up(self) -> bool:
        """Start the sidecar on a daemon thread; returns immediately.

        Returns True when already serving, False when a start was scheduled or
        the backend is disabled. Never raises and never waits for Node.
        """
        if self.is_ready():
            return True
        if not _env_flag("SATURN_MEMORY_ENABLED", "1") or _backend_disabled():
            return False
        if not _env_flag("SATURN_MEMORY_AUTOSTART", "1"):
            return False
        if time.monotonic() < self._start_cooldown_until:
            return False
        with self._lock:
            if self._warmup_thread is not None and self._warmup_thread.is_alive():
                return False
            self._warmup_thread = threading.Thread(
                target=self._warm_up_worker,
                name="saturn-memory-sidecar",
                daemon=True,
            )
            thread = self._warmup_thread
        thread.start()
        return False

    def _warm_up_worker(self) -> None:
        try:
            self.available(autostart=True)
        except Exception as exc:
            self._last_error = str(exc)
        finally:
            if not self.is_ready():
                # A failed Node start must not be retried on every memory
                # update; the JSON store is already safe in the meantime.
                self._start_cooldown_until = time.monotonic() + 30.0

    # --------------------------------------------------------------- sidecar
    def _embed_timeout_ms(self) -> int:
        return int(_as_float("SATURN_MEMORY_EMBED_TIMEOUT_MS", 3000.0, 250.0, 30000.0))

    def _sidecar_env(self) -> dict[str, str]:
        """Environment for the sidecar.

        Retrieval is always lexical: the assistant embeds nothing through an
        outside process, which is a fully supported mode of the engine.
        """
        env = os.environ.copy()
        settings = {
            "MEMORY_FILE": str(self.file),
            "MEMORY_PORT": str(self.port),
            "MEMORY_HOST": self.host,
            "MEMORY_KEY": self.api_key,
            "MEMORY_NAMESPACE": str(self.namespace),
            "MEMORY_EMBED_TIMEOUT_MS": str(self._embed_timeout_ms()),
        }
        # Growth controls the engine already implements; forwarded verbatim so
        # they can be tuned without a second implementation here.
        compact = _env_value("SATURN_MEMORY_COMPACT_BYTES")
        if compact:
            settings["MEMORY_COMPACT_BYTES"] = compact
        lock_stale = _env_value("SATURN_MEMORY_LOCK_STALE_MS")
        if lock_stale:
            settings["MEMORY_LOCK_STALE_MS"] = lock_stale

        settings["MEMORY_EMBED"] = "lexical"
        # The sidecar reads exactly one prefix, and it is set here.
        env.update({f"SATURN_{name}": value for name, value in settings.items()})
        return env

    def _claim_spawn(self) -> bool:
        """Take the per-store spawn lock, or report that someone else holds it.

        A lock file next to the store is the only thing that stops two
        processes from starting two engines over one JSONL file - the
        per-process revision counter cannot detect that. A stale lock (old
        timestamp, or a dead owner) is replaced instead of blocking startup
        forever.
        """
        path = self._spawn_lock
        for _attempt in range(2):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if not self._stale_spawn_lock(path):
                    return False
                try:
                    path.unlink()
                except Exception:
                    return False
                continue
            except Exception as exc:
                # A read-only workspace must not disable memory entirely: the
                # in-process lock still serializes this bridge.
                self._last_error = f"could not create the sidecar lock: {exc}"
                return True
            try:
                os.write(fd, json.dumps({
                    "pid": os.getpid(),
                    "port": self.port,
                    "ts": time.time(),
                }).encode("utf-8"))
            finally:
                os.close(fd)
            return True
        return False

    def _stale_spawn_lock(self, path: Path) -> bool:
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            ts = float(data.get("ts") or 0)
            pid = int(data.get("pid") or 0)
        except Exception:
            return True
        if time.time() - ts > 30:
            return True
        return not _pid_alive(pid)

    def _release_spawn_lock(self) -> None:
        try:
            self._spawn_lock.unlink(missing_ok=True)
        except Exception:
            pass

    def _start(self) -> bool:
        try:
            self._endpoint()
        except SaturnUnavailable as exc:
            # Refuse before Node is spawned: a sidecar told to listen on a
            # deceptive host would put the store on the wire the moment it
            # resolved that name, so "no traffic" has to include "no process".
            self._last_error = str(exc)
            return False
        node = _node_binary()
        if not node or not SIDECAR_JS.exists():
            self._last_error = "node or sidecar.js not found"
            return False

        # Resolve the token before probing: a sidecar this store already owns
        # was started with the persisted token, so probing without it would
        # answer 401 and look like a foreign process.
        if not self.api_key:
            self.api_key = _read_or_create_token()
            if self.api_key:
                print("[UniversalMemory] using a local auth token for the sidecar")

        # Reuse before spawning: a healthy sidecar is the only engine that may
        # serve this store, and a second engine on one file is a lost-update
        # bug the revision cannot see across processes.
        try:
            data = self._request("GET", "/status", timeout=0.6)
            if isinstance(data, dict) and data.get("ok"):
                self._status = data
                self._owns_process = False
                self._process = None
                self._available = True
                print(
                    "[UniversalMemory] reusing sidecar at "
                    f"{self.base_url} (mode={data.get('mode')}, "
                    f"entries={data.get('count')})"
                )
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._last_error = (
                    "a memory sidecar is already serving this port with a "
                    "different token; set SATURN_MEMORY_KEY or SATURN_MEMORY_KEY "
                    "to match it"
                )
                return False
        except Exception:
            pass
        if self.health(timeout=0.6):
            # An older sidecar (no /status) that answers /health is still the
            # engine for this store; adopt it without claiming ownership.
            self._owns_process = False
            self._process = None
            self._available = True
            return True

        if not self._claim_spawn():
            # Another process is starting the engine for this store. Give it a
            # moment rather than binding a second engine to the same file.
            deadline = time.time() + 6
            while time.time() < deadline:
                if self.health(timeout=0.5):
                    self._owns_process = False
                    self._process = None
                    self._available = True
                    return True
                time.sleep(0.2)
            self._last_error = "another process is starting the memory sidecar for this store"
            return False
        self._owns_process = True

        self.file.parent.mkdir(parents=True, exist_ok=True)
        env = self._sidecar_env()
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = subprocess.Popen(
                [node, str(SIDECAR_JS)],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except Exception as exc:
            self._last_error = str(exc)
            print(f"[UniversalMemory] sidecar start failed: {exc}")
            self.stop()
            return False

        deadline = time.time() + 8
        while time.time() < deadline:
            if self._process.poll() is not None:
                print("[UniversalMemory] sidecar exited during startup")
                self._last_error = "sidecar exited during startup"
                self.stop()
                return False
            if self.health(timeout=0.5):
                print(f"[UniversalMemory] sidecar ready at {self.base_url}")
                atexit.register(self.stop)
                self._status = self.status(timeout=1.0)
                if self._status:
                    print(
                        "[UniversalMemory] store status: "
                        f"mode={self._status.get('mode')}, "
                        f"entries={self._status.get('count')}, "
                        f"revision={self._status.get('revision')}, "
                        f"namespace={self._status.get('namespace')}"
                    )
                # Replay anything a previous run queued while the store was down.
                try:
                    self.flush_outbox()
                except Exception:
                    pass
                return True
            time.sleep(0.2)
        print("[UniversalMemory] sidecar startup timed out")
        self._last_error = "sidecar startup timed out"
        self.stop()
        return False

    def ollama_status(self, force: bool = False) -> dict:
        """Embeddings are lexical-only now; the external Ollama probe is gone.

        Kept as a static answer so ``diagnostics()`` retains its shape for
        consumers: the bridge talks to no process other than its own sidecar.
        The ``force`` parameter is accepted for call-site compatibility and
        intentionally does nothing.
        """
        return {
            "available": False,
            "reason": "disabled",
            "requested": "off",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }

    def stop(self) -> None:
        process, self._process = self._process, None
        self._available = False
        owned = self._owns_process
        self._owns_process = False
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        # Only the spawner releases the lock. An adopter (another instance that
        # reused this sidecar) must leave the store running for its owner.
        if owned:
            self._release_spawn_lock()

    # -------------------------------------------------------------- mapping
    @staticmethod
    def _entry_value(entry) -> str:
        if isinstance(entry, dict):
            return str(entry.get("value", "") or "").strip()
        return str(entry or "").strip()

    def _payload_for(self, category: str, key: str, entry) -> dict:
        return profile_payload(category, key, entry, namespace=self.namespace)

    def _session_payload(self, session, index: int = 0) -> dict:
        return session_payload(session, index=index, namespace=self.namespace)

    def _entry_payload(self, category: str, key: str, entry, index: int = 0) -> dict:
        if category == SESSION_CATEGORY:
            return session_payload(entry, index=index, namespace=self.namespace)
        return profile_payload(category, key, entry, namespace=self.namespace)

    def _desired_profile(self, memory: dict, include_sessions: bool) -> dict[tuple[str, str], dict]:
        """Profile -> ``{(category, key): native payload}`` in a stable order."""
        desired: dict[tuple[str, str], dict] = {}
        for category, items in (memory or {}).items():
            if category == SESSION_CATEGORY:
                continue
            if not isinstance(items, dict):
                continue
            for key, entry in items.items():
                if not self._entry_value(entry):
                    continue
                desired[(category, str(key))] = self._payload_for(category, key, entry)
        if include_sessions:
            sessions = (memory or {}).get(SESSION_CATEGORY, [])
            if isinstance(sessions, list):
                for index, session in enumerate(sessions):
                    payload = self._session_payload(session, index=index)
                    if not payload["meta"]["saturn_value"]:
                        continue
                    desired[(SESSION_CATEGORY, payload["meta"]["saturn_key"])] = payload
        return desired

    def _export_snapshot(self, timeout: float | None = None) -> tuple[dict, str | None]:
        """Namespace-scoped export plus the store revision it was read at.

        Every read that can lead to a write goes through here. The namespace
        parameter keeps a reconcile from ever *seeing* another namespace's rows
        (and therefore from deleting them), and the revision is the token a
        later compare-and-swap is checked against.
        """
        path = f"/export?namespace={quote(str(self.namespace), safe='')}"
        data = self._request("GET", path, timeout=timeout or self.recall_timeout)
        if not isinstance(data, dict):
            data = {"entries": []}
        revision = str(data.get("revision") or "").strip() or None
        return data, revision

    def _scoped_entries(self, data: dict) -> list[dict]:
        """Entries this store owns.

        Rows written before namespaces existed carry no field and belong to the
        store; a row that names a *different* namespace is never visible, even
        if the transport ignored the scope parameter.
        """
        entries: list[dict] = []
        for entry in (data.get("entries") or []) if isinstance(data, dict) else []:
            if not isinstance(entry, dict):
                continue
            actual = str(entry.get("namespace") or "").strip()
            if actual and actual != self.namespace:
                continue
            entries.append(entry)
        return entries

    def _group_identities(self, data: dict) -> dict[tuple[str, str], list[dict]]:
        """Mirrored/native entries grouped by their Saturn identity.

        The value is a list on purpose: an interrupted mirror, a manual edit,
        or a pre-metadata-era entry can leave two active rows for one identity.
        Sync keeps the newest and removes the rest, so ``all_entries_for_ui``
        can never show the same fact twice.
        """
        grouped: dict[tuple[str, str], list[dict]] = {}
        for position, entry in enumerate(self._scoped_entries(data)):
            meta = entry.get("meta") or {}
            category = str(meta.get("saturn_category") or "").strip()
            key = str(meta.get("saturn_key") or "").strip()
            if not category or not key:
                continue
            grouped.setdefault((category, key), []).append({
                "id": entry.get("id"),
                "value": str(meta.get("saturn_value", "") or ""),
                "updated": str(meta.get("saturn_updated", "") or ""),
                "language": str(meta.get("saturn_language", "") or ""),
                "at": str(entry.get("at") or ""),
                "position": position,
            })
        for rows in grouped.values():
            rows.sort(key=lambda row: (row.get("at") or "", row.get("position") or 0))
        return grouped

    def _existing(self, timeout: float | None = None) -> dict[tuple[str, str], list[dict]]:
        """Namespace-scoped identity index (see :meth:`_export_snapshot`)."""
        data, _revision = self._export_snapshot(timeout=timeout)
        return self._group_identities(data)

    def _patch(
        self,
        revision: str,
        writes: list[dict],
        forgets: list[str],
        timeout: float | None = None,
    ) -> dict:
        """Apply a diff computed from ``revision``; 409 means it went stale."""
        try:
            data = self._request(
                "POST",
                "/patch",
                {
                    "expectedRevision": revision,
                    "namespace": self.namespace,
                    "remember": list(writes),
                    "forget": list(forgets),
                },
                timeout=timeout or self.write_timeout,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise SaturnStaleSnapshot(
                    "the native memory store changed since this snapshot was read; "
                    "nothing was written"
                ) from exc
            raise
        rejected = (data or {}).get("rejected") or [] if isinstance(data, dict) else []
        if rejected:
            reasons = ", ".join(str((item or {}).get("reason") or "entry") for item in rejected[:3])
            raise SaturnWriteError(f"Saturn rejected part of the write: {reasons}")
        return data if isinstance(data, dict) else {}

    def _sync(
        self,
        memory: dict,
        *,
        timeout: float | None = None,
        autostart: bool = False,
        include_sessions: bool = False,
        strict: bool = False,
        expected_revision: str | None = None,
    ) -> dict | None:
        """Reconcile the native store with ``memory``.

        ``strict`` is the authority switch: it raises instead of returning
        ``None`` and never touches ``long_term.json``. Reconcile is a diff, so
        a repeated call with unchanged data performs zero writes.

        ``expected_revision`` turns the write into a compare-and-swap against
        the snapshot the caller actually read: if the store moved since then,
        :class:`SaturnStaleSnapshot` is raised instead of applying a diff that
        would erase somebody else's committed write.
        """
        if not isinstance(memory, dict):
            if strict:
                raise SaturnWriteError("memory payload must be a dict")
            return None

        if not self.available(autostart=autostart):
            if not autostart:
                self.warm_up()
            self._shadow_status = "pending"
            if strict:
                raise SaturnUnavailable(
                    f"Saturn memory backend is not serving at {self.base_url}"
                    + (f" ({self._last_error})" if self._last_error else "")
                )
            return None

        deadline = time.monotonic() + max(0.5, float(timeout or self.write_timeout))

        def remaining() -> float:
            return max(0.25, deadline - time.monotonic())

        def check_deadline() -> None:
            if time.monotonic() >= deadline:
                raise SaturnWriteError("Saturn write deadline exceeded")

        def forget(entry_id: str) -> None:
            if not entry_id:
                return
            check_deadline()
            self._request("DELETE", f"/memory/{entry_id}", timeout=remaining())

        summary = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "deduped": 0}

        existing_data, export_revision = self._export_snapshot(timeout=remaining())
        existing = self._group_identities(existing_data)
        desired = self._desired_profile(memory, include_sessions=include_sessions)

        writes: list[tuple[tuple[str, str], dict]] = []
        forgets: list[str] = []

        for identity, payload in desired.items():
            rows = existing.get(identity, [])
            wanted_value = payload["meta"]["saturn_value"]
            wanted_updated = payload["meta"].get("saturn_updated") or ""
            fresh = [
                row for row in rows
                if row.get("value") == wanted_value and (not wanted_updated or row.get("updated") == wanted_updated)
            ]
            if fresh:
                keep = fresh[-1]
                for row in rows:
                    if row is not keep:
                        forgets.append(str(row.get("id") or ""))
                        summary["deduped"] += 1
                summary["unchanged"] += 1
                continue

            writes.append((identity, payload))
            summary["updated" if rows else "created"] += 1
            for row in rows:
                forgets.append(str(row.get("id") or ""))

        # Entries the profile no longer contains. Sessions are only pruned when
        # the caller owns them (full mode); a hybrid mirror never deletes them.
        for identity, rows in existing.items():
            if identity in desired:
                continue
            if identity[0] == SESSION_CATEGORY and not include_sessions:
                continue
            for row in rows:
                forgets.append(str(row.get("id") or ""))
                summary["deleted"] += 1

        forgets = [entry_id for entry_id in forgets if entry_id]
        revision = expected_revision or export_revision

        if revision:
            if writes or forgets:
                try:
                    self._patch(
                        revision,
                        [payload for _identity, payload in writes],
                        forgets,
                        timeout=remaining(),
                    )
                except SaturnStaleSnapshot:
                    if strict or expected_revision:
                        raise
                    # Mirror mode owns no authority: re-read once and reconcile
                    # against the newer snapshot instead of reporting failure.
                    return self._sync(
                        memory,
                        timeout=timeout,
                        autostart=autostart,
                        include_sessions=include_sessions,
                        strict=strict,
                    )
        else:
            # Transport without a revision (older sidecar or a test double):
            # fall back to per-entry writes, which cannot detect a stale
            # snapshot but keeps the mirror working.
            for identity, payload in writes:
                check_deadline()
                result = self._request("POST", "/remember", payload, timeout=remaining())
                new_id = str((result or {}).get("id") or "") if isinstance(result, dict) else ""
                if not new_id:
                    raise SaturnWriteError(f"Saturn did not persist {identity[0]}/{identity[1]}")
            for entry_id in forgets:
                forget(entry_id)

        self._shadow_status = "synced"
        self._last_error = ""
        return summary

    # ---------------------------------------------------------------- outbox
    #
    # A strict write that fails because the sidecar is briefly away is queued
    # next to the store and replayed on the next healthy write or startup. This
    # is not a second authority: every replay is a compare-and-swap against the
    # revision the intent was computed from, so a stale intent is dropped rather
    # than resurrected. Only a bridge that *owns* the sidecar queues, so an
    # externally started store never gains a file it did not ask for.

    def _outbox_path(self) -> Path:
        return Path(str(self.file) + ".outbox.json")

    def _read_outbox(self) -> list[dict]:
        try:
            data = json.loads(self._outbox_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            return []
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def _write_outbox(self, rows: list[dict]) -> None:
        path = self._outbox_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(rows[:OUTBOX_LIMIT], ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def pending_writes(self) -> int:
        return len(self._read_outbox())

    def _queue_write(self, memory: dict, *, expected_revision, include_sessions, error) -> None:
        if not self._owns_process or not _env_flag("SATURN_MEMORY_OUTBOX", "1"):
            return
        if expected_revision is None:
            # A plain-dict write carries no snapshot revision. Capture the
            # current one so the replay is still a compare-and-swap; if even
            # that read fails there is no safe revision, and the record is
            # dropped at replay instead of clobbering a newer write.
            try:
                _data, current = self._export_snapshot(timeout=self.recall_timeout)
                expected_revision = current
            except Exception:
                expected_revision = None
        record = {
            "profile": dict(memory),
            "expected_revision": expected_revision,
            "include_sessions": bool(include_sessions),
            "namespace": self.namespace,
            "file": str(self.file),
            "queued_at": datetime.now().isoformat(timespec="seconds"),
            "error": str(error or ""),
        }
        with self._outbox_lock:
            rows = self._read_outbox()
            rows.append(record)
            try:
                self._write_outbox(rows)
            except Exception as exc:
                print(f"[UniversalMemory] could not queue the failed write: {exc}")

    def flush_outbox(self, timeout: float | None = None) -> dict:
        """Replay queued strict writes against the current store.

        Every record is applied through the same compare-and-swap as a live
        write, so a record computed from a stale revision is dropped (a newer
        write already won) instead of overwriting it.
        """
        with self._outbox_lock:
            rows = self._read_outbox()
            if not rows:
                return {"replayed": 0, "dropped": 0, "pending": 0}
            kept: list[dict] = []
            replayed = dropped = 0
            for row in rows:
                if (
                    not _same_path(row.get("file"), self.file)
                    or str(row.get("namespace") or "") != self.namespace
                ):
                    dropped += 1
                    continue
                profile = row.get("profile")
                if not isinstance(profile, dict):
                    dropped += 1
                    continue
                expected = row.get("expected_revision") or None
                if not expected:
                    # No provable revision: replaying would be a blind write.
                    dropped += 1
                    continue
                try:
                    self._sync(
                        profile,
                        timeout=timeout,
                        autostart=False,
                        include_sessions=bool(row.get("include_sessions")),
                        strict=True,
                        expected_revision=expected,
                    )
                    replayed += 1
                except SaturnStaleSnapshot:
                    dropped += 1
                except Exception:
                    kept.append(row)
            try:
                self._write_outbox(kept)
            except Exception as exc:
                print(f"[UniversalMemory] could not update the outbox: {exc}")
            return {"replayed": replayed, "dropped": dropped, "pending": len(kept)}

    # ------------------------------------------------------------ diagnostics
    def drift_report(
        self,
        profile: dict,
        timeout: float | None = None,
        include_sessions: bool = False,
        sample: int = 20,
    ) -> dict:
        """Read-only comparison of an expected profile with the native store.

        ``hybrid`` mirrors JSON into the native store, so the only way to know
        the mirror did not silently diverge is to compare identity by identity.
        Nothing is written; a reconciler can act on the report.
        """
        try:
            data, revision = self._export_snapshot(timeout=timeout or self.recall_timeout)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "namespace": self.namespace}
        existing = self._group_identities(data)
        desired = self._desired_profile(
            profile if isinstance(profile, dict) else {}, include_sessions=include_sessions
        )
        missing: list[dict] = []
        changed: list[dict] = []
        for identity, payload in desired.items():
            rows = existing.get(identity) or []
            if not rows:
                missing.append({"category": identity[0], "key": identity[1]})
                continue
            wanted = payload["meta"]["saturn_value"]
            wanted_updated = payload["meta"].get("saturn_updated") or ""
            if not any(
                row.get("value") == wanted
                and (not wanted_updated or row.get("updated") == wanted_updated)
                for row in rows
            ):
                changed.append({
                    "category": identity[0],
                    "key": identity[1],
                    "expected": wanted,
                    "found": rows[-1].get("value") if rows else "",
                })
        extra = [
            {"category": identity[0], "key": identity[1]}
            for identity in existing
            if identity not in desired
            and not (identity[0] == SESSION_CATEGORY and not include_sessions)
        ]
        counts = {"missing": len(missing), "changed": len(changed), "extra": len(extra)}
        return {
            "ok": not any(counts.values()),
            "namespace": self.namespace,
            "revision": revision,
            "counts": counts,
            "missing": missing[:sample],
            "changed": changed[:sample],
            "extra": extra[:sample],
        }

    def diagnostics(self, profile: dict | None = None, timeout: float | None = None) -> dict:
        """Health plus, when a profile is supplied, a hybrid drift report."""
        report = self.health_report(timeout=timeout or 1.5)
        if profile is not None:
            report["drift"] = self.drift_report(profile, timeout=timeout)
        return report

    def save_memory(
        self,
        memory: dict,
        timeout: float | None = None,
        autostart: bool = False,
    ) -> bool:
        """Mirror a JSON-owned profile into Saturn (Stage One behaviour).

        Best effort by contract: the JSON write already happened, so failures
        are reported and retried by the next save instead of raised.
        """
        try:
            summary = self._sync(memory, timeout=timeout, autostart=autostart)
            return summary is not None
        except Exception as exc:
            print(f"[UniversalMemory] mirror failed; long_term.json kept: {exc}")
            self._last_error = str(exc)
            self._shadow_status = "failed"
            # Let the next save re-check health; the Node process may still be
            # alive after a per-request timeout.
            self._available = False
            return False

    def snapshot_scope(
        self,
        profile,
        expected_revision: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Whether ``profile`` provably describes *this* bridge's destination.

        A revision is only half of a compare-and-swap. It says "the store did
        not move", never "this snapshot came from that store" - and a revision
        from namespace A can easily equal one from namespace B. So the write
        path validates the snapshot's provenance (the namespace and store it
        was read from) against the destination this bridge is verified to be
        serving, before any diff is computed:

        * the token's namespace must be the namespace this bridge writes into;
        * the token's store must be the file ``/diagnostics`` reports (falling
          back to the configured file when the sidecar is too quiet to verify,
          which is only possible for a request that cannot be served anyway);
        * a caller-supplied ``expected_revision`` must agree with the revision
          the token was minted with.

        Returns ``{"ok", "issued", "reason", "snapshot", "destination"}``.
        ``issued`` is false for a plain dict - a mirror write with no
        provenance - which has nothing to validate and is the caller's business.
        """
        state = snapshot_state(profile)
        result = {
            "ok": True,
            "issued": bool(state.get("issued")),
            "reason": "",
            "snapshot": {
                "namespace": state.get("namespace"),
                "file": state.get("file"),
                "revision": state.get("revision"),
            },
            "destination": None,
        }
        if not state.get("issued"):
            return result
        try:
            destination = self.destination_identity(verify=True, timeout=timeout)
        except Exception as exc:  # pragma: no cover - destination_identity never raises
            result.update(ok=False, reason=f"destination identity unavailable: {exc}")
            return result
        result["destination"] = {
            "engine_file": destination.get("engine_file"),
            "namespace": destination.get("namespace"),
            "verified": bool(destination.get("verified")),
            "mismatch": destination.get("mismatch") or "",
        }
        if destination.get("mismatch"):
            result.update(ok=False, reason=str(destination["mismatch"]))
            return result

        token_namespace = str(state.get("namespace") or "").strip()
        destination_namespace = str(destination.get("namespace") or "").strip()
        if token_namespace and destination_namespace and token_namespace != destination_namespace:
            result.update(
                ok=False,
                reason=(
                    f"the snapshot was read from namespace {token_namespace!r} but this "
                    f"bridge writes to {destination_namespace!r}"
                ),
            )
            return result

        token_file = str(state.get("file") or "").strip()
        destination_file = str(destination.get("engine_file") or "").strip()
        if token_file and destination_file and not _same_path(token_file, destination_file):
            result.update(
                ok=False,
                reason=(
                    f"the snapshot was read from store {token_file!r} but the serving "
                    f"store for this bridge is {destination_file!r}"
                ),
            )
            return result

        token_revision = state.get("revision")
        if expected_revision and token_revision and str(token_revision) != str(expected_revision):
            result.update(
                ok=False,
                reason=(
                    f"the snapshot revision {token_revision!r} does not match the "
                    f"revision this write would compare against {str(expected_revision)!r}"
                ),
            )
            return result
        return result

    def write_profile(
        self,
        memory: dict,
        timeout: float | None = None,
        autostart: bool = True,
        include_sessions: bool = True,
        expected_revision: str | None = None,
    ) -> dict:
        """Authoritative write (Stage Two). Raises instead of degrading.

        This boundary is self-sufficient: a direct caller gets the same
        guarantees as ``memory_manager`` without having to reproduce any of
        them.

        * a snapshot this process issued from a *failed* read
          (:class:`ProfileOutage`), and a ``ProfileSnapshot`` built by hand
          instead of read, are refused here with
          :class:`SaturnSnapshotScopeError` before a byte is sent - so an
          outage can never be replayed as "the user deleted everything";
        * the compare-and-swap revision is taken from the snapshot's own token,
          so a caller that forgets ``expected_revision`` still gets a real CAS:
          :class:`SaturnStaleSnapshot` is raised instead of applying a diff
          computed from contents the store has since moved past;
        * the snapshot's *scope* (namespace and store) is validated against the
          destination this bridge is verified to be serving, so a snapshot read
          from another namespace or another store is refused even when its
          revision happens to match.

        A plain dict carries no provenance - the hybrid mirror and the
        reconcilers pass transport reads - so there is nothing to validate and
        its issuance remains the caller's business.
        """
        state = snapshot_state(memory)
        issued = isinstance(memory, ProfileSnapshot) or bool(state.get("issued"))
        if issued and not state["ok"]:
            raise SaturnSnapshotScopeError(
                "refusing to overwrite the native memory store: "
                f"{state['reason']}. Nothing was written."
            )
        # The token *is* the CAS input: deriving it here means a direct caller
        # cannot turn an authoritative write into a blind overwrite by omitting
        # the keyword.
        effective_revision = expected_revision or (state.get("revision") if state["ok"] else None)
        scope = self.snapshot_scope(
            memory, expected_revision=effective_revision, timeout=timeout
        )
        if not scope["ok"]:
            raise SaturnSnapshotScopeError(
                "refusing to overwrite the native memory store: "
                f"{scope['reason']}. Nothing was written."
            )
        # Replay anything a previous outage queued, then write. A replay is a
        # compare-and-swap too, so it can only help convergence; a failure here
        # must never block the live write.
        try:
            self.flush_outbox(timeout=timeout)
        except Exception:
            pass
        try:
            summary = self._sync(
                memory,
                timeout=timeout,
                autostart=autostart,
                include_sessions=include_sessions,
                strict=True,
                expected_revision=effective_revision,
            )
        except (SaturnSnapshotScopeError, SaturnStaleSnapshot):
            # A refused or stale snapshot is a real answer, not an outage: it
            # must reach the caller so it re-reads instead of being queued.
            raise
        except SaturnError as exc:
            self._queue_write(
                memory,
                expected_revision=effective_revision,
                include_sessions=include_sessions,
                error=exc,
            )
            raise
        except Exception as exc:
            self._available = False
            error = SaturnWriteError(f"Saturn write failed: {exc}")
            self._queue_write(
                memory,
                expected_revision=effective_revision,
                include_sessions=include_sessions,
                error=error,
            )
            raise error from exc
        if summary is None:  # defensive: strict sync never returns None
            raise SaturnWriteError("Saturn write did not complete")
        return summary

    def load_memory(self, timeout: float | None = None, include_sessions: bool = True) -> dict:
        """Native state rendered in the Saturn profile shape.

        A successful read returns a :class:`ProfileSnapshot`: it carries the
        revision and scope of *this* read, which is what a later authoritative
        write is checked against. Provenance is per snapshot, never a global
        flag, so a stale snapshot stays stale even after a newer read succeeded.

        A failed read returns a :class:`ProfileOutage` - the same dict shape,
        with failed-read provenance and no revision - instead of a plain empty
        dict. Callers keep reading a profile, but no write path can mistake the
        outage for "the user deleted everything".
        """
        try:
            data, revision = self._export_snapshot(timeout=timeout or self.recall_timeout)
        except Exception as exc:
            return ProfileOutage(
                str(exc) or type(exc).__name__,
                namespace=self.namespace,
                file=self.file,
            )
        # Writable evidence exists only here: the export above answered, so
        # this exact object may be written back. No public constructor can
        # produce the same thing.
        memory = _verified_snapshot(
            {category: {} for category in _BASE_MEMORY},
            revision=revision,
            namespace=self.namespace,
            file=str(self.file),
            source="saturn",
            proof=_VERIFIED_READ,
        )
        if include_sessions:
            memory[SESSION_CATEGORY] = []
        for entry in self._scoped_entries(data):
            meta = entry.get("meta") or {}
            category = str(meta.get("saturn_category") or "")
            key = str(meta.get("saturn_key") or "")
            value = str(meta.get("saturn_value", "") or "").strip()
            if not category or not key or not value:
                continue
            if category == SESSION_CATEGORY:
                if not include_sessions:
                    continue
                session = {
                    "date": str(meta.get("saturn_updated", "") or "") or str(entry.get("at") or "")[:10],
                    "summary": value,
                }
                language = str(meta.get("saturn_language", "") or "").strip()
                if language:
                    session["language"] = language
                memory[SESSION_CATEGORY].append(session)
                continue
            if category not in memory:
                continue
            memory[category][key] = {
                "value": value,
                "updated": str(meta.get("saturn_updated", "") or ""),
            }
        if include_sessions:
            # Export order is insertion order, which is the session order;
            # sorting by date alone keeps that order stable within a day.
            memory[SESSION_CATEGORY].sort(key=lambda item: item.get("date") or "")
        self._last_error = ""
        return memory


    def recall_entries(
        self,
        query: str,
        limit: int = 8,
        timeout: float | None = None,
        min_score: float | None = None,
        kinds: list[str] | None = None,
    ) -> list[dict]:
        """Structured native recall hits for Saturn's merge layer.

        Mirrored entries carry stable Saturn metadata, so the merge can dedupe
        by (category, key). Entries without metadata are still returned; the
        merge falls back to a value fingerprint for those.

        Stage Three adds two optional filters and richer rows. ``min_score`` and
        ``kinds`` are forwarded to the engine (it already supports both), so
        filtering happens in the one place that ranks. Cortex entries report
        their own category/key so a native-only store still renders as facts
        instead of opaque ``saturn/memory`` rows.
        """
        text = str(query or "").strip()
        if not text:
            return []
        payload: dict = {
            "query": text,
            "k": max(1, min(int(limit or 1), 50)),
            "namespace": self.namespace,
        }
        if min_score is not None:
            payload["minScore"] = max(0.0, min(float(min_score), 1.0))
        if kinds:
            payload["kinds"] = [str(kind) for kind in kinds]
        data = self._request(
            "POST",
            "/recall",
            payload,
            timeout=timeout or self.recall_timeout,
        )
        rows: list[dict] = []
        for hit in data.get("hits") or []:
            if not isinstance(hit, dict) or not self._hit_in_scope(hit):
                continue
            meta = hit.get("meta") if isinstance(hit.get("meta"), dict) else {}
            category = str(meta.get("saturn_category") or "").strip()
            key = str(meta.get("saturn_key") or meta.get("predicate") or "").strip()
            value = meta.get("saturn_value")
            value = str(value).strip() if value is not None else ""
            body = str(hit.get("text") or "").strip()
            cortex = bool(meta.get("cortex"))
            row = {
                "id": str(hit.get("id") or ""),
                "kind": str(hit.get("kind") or ""),
                "category": category,
                "key": key,
                "value": value or body,
                "text": body,
                "has_saturn_meta": bool(category and key),
                "score": _as_number(hit.get("score")),
                "relevance": _as_number(hit.get("relevance")),
                "importance": _as_number(hit.get("importance")),
                "access_count": int(_as_number(hit.get("accessCount"))),
                "created_at": str(hit.get("at") or ""),
                "meta": meta,
                "cortex": cortex,
            }
            if cortex:
                row["category"] = str(meta.get("cortex_category") or "cortex").strip() or "cortex"
                row["key"] = str(meta.get("cortex_key") or "memory").strip() or "memory"
                row["value"] = str(meta.get("cortex_content") or body).strip() or body
            rows.append(row)
        return rows

    # ------------------------------------------------- native operations
    def require(self) -> None:
        """Raise :class:`SaturnUnavailable` unless the sidecar is serving.

        The authoritative adapters call this before they read-modify-write, so
        a cold backend can never be mistaken for an empty store that is then
        overwritten.
        """
        if not self.available(autostart=True):
            raise SaturnUnavailable(
                f"Saturn memory backend is not serving at {self.base_url}"
                + (f" ({self._last_error})" if self._last_error else "")
            )

    def _hit_in_scope(self, entry: dict) -> bool:
        """True when a read hit belongs to this bridge's namespace."""
        actual = str((entry or {}).get("namespace") or "").strip()
        return not actual or actual == self.namespace

    def list_entries(
        self,
        limit: int = 200,
        offset: int = 0,
        namespace: str | None = None,
        include_sessions: bool = True,
        timeout: float | None = None,
        include_history: bool = False,
    ) -> dict:
        """Paged native listing, scoped to this bridge's namespace by default.

        ``include_history`` also returns rows whose validity window has closed.
        Recall never shows them, so a caller that only mirrors what the app can
        read leaves this off; a caller that has to account for every stored row,
        such as the namespace migration, turns it on.
        """
        wanted = str(namespace or self.namespace)
        params = [
            f"limit={max(1, int(limit or 1))}",
            f"offset={max(0, int(offset or 0))}",
            f"namespace={quote(wanted, safe='')}",
        ]
        if include_sessions:
            params.append("includeSessions=1")
        if include_history:
            params.append("includeHistory=1")
        data = self._request("GET", "/entries?" + "&".join(params), timeout=timeout or self.recall_timeout)
        entries = data.get("entries") if isinstance(data, dict) else None
        rows = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            actual = str(entry.get("namespace") or "").strip()
            if actual and actual != wanted:
                continue
            rows.append(entry)
        return {
            "entries": rows,
            "total": int((data or {}).get("total") or 0) if isinstance(data, dict) else 0,
            "mode": str((data or {}).get("mode") or "") if isinstance(data, dict) else "",
            "namespace": wanted,
        }

    def native_index(self, timeout: float | None = None) -> dict[tuple[str, str], list[dict]]:
        """Public view of the identity index used by sync and the importer."""
        return self._existing(timeout=timeout)

    def export_entries(self, timeout: float | None = None) -> dict:
        """Raw engine export - the migration and rollback artifact source.

        Scoped to this namespace on purpose: a migration or rollback artifact
        that contained another namespace's rows would import them into the
        wrong store on restore.
        """
        data, revision = self._export_snapshot(timeout=timeout or self.recall_timeout)
        payload = dict(data)
        payload["entries"] = self._scoped_entries(data)
        if revision:
            payload["revision"] = revision
        payload.setdefault("namespace", self.namespace)
        return payload

    def import_entries(
        self,
        entries: list[dict],
        timeout: float | None = None,
        batch_bytes: int | None = None,
        preserve_ids: bool | None = None,
    ) -> dict:
        """Bulk import into *this* namespace.

        The namespace travels in the body and every entry is stamped with it,
        so a snapshot exported from another destination cannot land in the
        wrong store.

        ``preserve_ids`` is left unset by default, which keeps the engine's own
        default (preserve). A cross-namespace copy must pass ``False``: the
        engine's id index is global to the file, so an id that already exists
        in the source namespace is skipped rather than copied.

        The sidecar caps a request body at 256 KiB, while a migrated database
        can be far larger, so entries are split into batches by their
        *serialized* size and sent one at a time. Every batch is verified and
        the counts are accumulated: a batch that does not account for each of
        its entries raises instead of leaving a partial import looking like a
        success. Callers only write a migration marker after this returns, so
        an interrupted import is retried rather than recorded.
        """
        stamped = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            stamped.append({**entry, "namespace": self.namespace})
        if not stamped:
            return {"ok": True, "imported": 0, "skipped": 0, "batches": 0}

        limit = int(batch_bytes or IMPORT_BATCH_BYTES)
        imported = 0
        skipped = 0
        batches = 0
        for batch in _batched_entries(stamped, limit):
            body = {"entries": batch, "namespace": self.namespace}
            if preserve_ids is not None:
                body["preserveIds"] = bool(preserve_ids)
            data = self._request(
                "POST",
                "/import",
                body,
                timeout=timeout or self.write_timeout,
            )
            if not isinstance(data, dict) or data.get("ok") is False:
                raise SaturnWriteError(
                    f"Saturn rejected import batch {batches + 1}: "
                    f"{(data or {}).get('error') if isinstance(data, dict) else data}"
                )
            got_imported = int(data.get("imported") or 0)
            got_skipped = int(data.get("skipped") or 0)
            if got_imported + got_skipped != len(batch):
                # Covers both a silent partial write and an error payload that
                # carries counts but no ``ok``: either way the batch did not
                # account for what was sent, so it is not a success.
                raise SaturnWriteError(
                    f"Saturn import batch {batches + 1} accounted for "
                    f"{got_imported + got_skipped} of {len(batch)} entries"
                )
            imported += got_imported
            skipped += got_skipped
            batches += 1
        return {
            "ok": True,
            "imported": imported,
            "skipped": skipped,
            "batches": batches,
        }

    # ------------------------------------------- Stage Three native calls
    #
    # Thin transports only. Dedupe, merge, contradiction handling, ranking and
    # extraction all stay in the engine, so ``core/memory_cortex.py`` can be a
    # facade without becoming a second memory implementation.

    def remember_entry(self, payload: dict, timeout: float | None = None) -> dict:
        """Write one native entry and return the engine result unchanged.

        The engine answers ``{id, entry, merged}`` or ``{error}``; callers must
        treat a missing ``id`` as a failed write rather than a silent success.

        The entry is stamped with *this* bridge's namespace, overwriting any
        value the payload carries. A payload built elsewhere - a migration
        artifact exported from another store, or a process whose environment
        disagrees with the connection - must not be able to place a row where
        this reader will never look.
        """
        if not isinstance(payload, dict) or not str(payload.get("text") or "").strip():
            return {"error": "EMPTY"}
        body = {**payload, "namespace": self.namespace}
        data = self._request("POST", "/remember", body, timeout=timeout or self.write_timeout)
        return data if isinstance(data, dict) else {}

    def ingest_file(
        self,
        file_path: str,
        title: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Read a local document into memory and return the engine's answer.

        The file is parsed inside the sidecar, where the OCR hooks live, so this
        is the only route by which a screenshot or a scanned PDF becomes memory:
        the engine's own ingest routes take text that someone else already
        extracted. The answer is metadata (``docId``, ``title``, ``chunks``) and
        never the text itself, so a caller that wants the words recalls them.

        Failure is returned rather than raised. A missing file, an unreadable
        scan and a host without Tesseract are all ordinary outcomes of "remember
        this", and the engine's ``message`` is the part worth repeating to a
        user - which is why the status code travels with the body instead of
        being turned into an ``HTTPError``.
        """
        target = str(file_path or "").strip()
        if not target:
            return {"error": "PATH_REQUIRED"}
        if not os.path.isabs(target):
            target = os.path.abspath(target)
        payload: dict = {"path": target, "namespace": self.namespace}
        if title:
            payload["title"] = str(title)
        try:
            status, data = self._request_raw(
                "POST", "/ingest/file", payload, timeout=timeout or self.ingest_timeout
            )
        except (OSError, http.client.HTTPException) as exc:
            # The sidecar parses the file itself, so an unreachable store means
            # no parse happened at all - never a half-ingested document.
            self._last_error = str(exc)
            return {"error": "UNREACHABLE", "message": str(exc), "path": target}
        if not isinstance(data, dict):
            data = {}
        if status >= 300 or data.get("error"):
            self._last_error = str(data.get("error") or f"HTTP {status}")
            return {**data, "ok": False}
        return data

    def documents(self, timeout: float | None = None) -> list[dict]:
        """Documents this store holds, newest first. Empty on any failure.

        Reads through the engine's own route, so the namespace is applied by the
        store rather than by the caller: a namespace with no documents gets an
        empty list even when the file holds someone else's.
        """
        path = "/documents?namespace=" + quote(str(self.namespace), safe="")
        try:
            data = self._request("GET", path, timeout=timeout or self.recall_timeout)
        except Exception:
            return []
        rows = data.get("documents") if isinstance(data, dict) else None
        return [row for row in (rows or []) if isinstance(row, dict)]

    def forget_document(self, doc_id: str, timeout: float | None = None) -> bool:
        """Delete every chunk of one document, scoped to this namespace."""
        target = str(doc_id or "").strip()
        if not target:
            return False
        path = (
            f"/documents/{quote(target, safe='')}"
            f"?namespace={quote(str(self.namespace), safe='')}"
        )
        try:
            status, _data = self._request_raw(
                "DELETE", path, None, timeout=timeout or self.write_timeout
            )
        except (OSError, http.client.HTTPException):
            return False
        return status < 300

    def extract_candidates(
        self,
        text: str,
        limit: int = 5,
        timeout: float | None = None,
    ) -> list[dict]:
        """Extraction candidates from the engine's own extractor (no model).

        Returns an empty list on any failure: extraction is an enhancement, and
        the deterministic harvest patterns remain the guaranteed path.
        """
        source = str(text or "").strip()
        if not source:
            return []
        try:
            data = self._request(
                "POST",
                "/extract",
                {"text": source, "limit": max(1, min(int(limit or 1), 20))},
                timeout=timeout or self.recall_timeout,
            )
        except Exception:
            return []
        rows = data.get("candidates") if isinstance(data, dict) else None
        return [row for row in (rows or []) if isinstance(row, dict)]

    def feedback_entry(
        self,
        entry_id: str,
        delta: float = 0.03,
        timeout: float | None = None,
    ) -> bool:
        """Nudge an existing entry's importance in place (engine ``/feedback``).

        The engine's ``feedback`` is namespace-blind (it looks the id up in a
        global map), so the namespace travels with the request and the sidecar
        refuses an id that belongs to another store. Without both halves a
        cortex harvest could reinforce - and thereby keep alive - a row this
        bridge will never read again.
        """
        if not entry_id:
            return False
        try:
            data = self._request(
                "POST",
                "/feedback",
                {
                    "id": str(entry_id),
                    "useful": True,
                    "delta": max(0.0, min(float(delta), 1.0)),
                    "namespace": self.namespace,
                },
                timeout=timeout or self.write_timeout,
            )
        except Exception:
            return False
        return bool(data.get("ok")) if isinstance(data, dict) else False

    def get_entry(self, entry_id: str, timeout: float | None = None) -> dict | None:
        """One entry, only if it belongs to this bridge's namespace.

        The engine's ``get`` is namespace-blind, so the check is enforced here
        as well as in the sidecar: a caller can never read - and then delete -
        a row that belongs to another store.
        """
        if not entry_id:
            return None
        try:
            data = self._request(
                "GET",
                f"/memory/{quote(str(entry_id), safe='')}?namespace={quote(self.namespace, safe='')}",
                timeout=timeout or self.recall_timeout,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        entry = data.get("entry") if isinstance(data, dict) else None
        if not isinstance(entry, dict) or not self._hit_in_scope(entry):
            return None
        return entry

    def delete_entry(self, entry_id: str, timeout: float | None = None) -> bool:
        """Delete one entry, refusing anything outside this namespace."""
        if not entry_id:
            return False
        entry = self.get_entry(entry_id, timeout=timeout)
        if entry is None:
            return False
        try:
            self._request(
                "DELETE",
                f"/memory/{quote(str(entry_id), safe='')}",
                {"namespace": self.namespace},
                timeout=timeout or self.write_timeout,
            )
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    def native_context(
        self,
        query: str,
        token_budget: int = 600,
        limit: int = 8,
        timeout: float | None = None,
    ) -> dict:
        """Bounded native context assembly for the full-mode recall merge.

        ``perSubject`` is disabled so two different predicates on the same
        subject are not collapsed, and reinforcement is left to the engine
        (context assembly already recalls without reinforcing).
        """
        text = str(query or "").strip()
        if not text:
            return {"text": "", "rows": [], "tokens": 0, "mode": ""}
        data = self._request(
            "POST",
            "/context",
            {
                "query": text,
                "k": max(4, min(int(limit or 8) * 2, 64)),
                "tokenBudget": max(120, min(int(token_budget or 600), 4000)),
                "perSubject": 0,
                "format": "text",
                "namespace": self.namespace,
            },
            timeout=timeout or self.recall_timeout,
        )
        rows: list[dict] = []
        for hit in (data.get("entries") or []) if isinstance(data, dict) else []:
            if not isinstance(hit, dict) or not self._hit_in_scope(hit):
                continue
            meta = hit.get("meta") if isinstance(hit.get("meta"), dict) else {}
            category = str(meta.get("saturn_category") or "").strip()
            key = str(meta.get("saturn_key") or meta.get("predicate") or "").strip()
            value = meta.get("saturn_value")
            value = str(value).strip() if value is not None else ""
            body = str(hit.get("text") or "").strip()
            cortex = bool(meta.get("cortex"))
            if cortex:
                category = str(meta.get("cortex_category") or "cortex").strip() or "cortex"
                key = str(meta.get("cortex_key") or "memory").strip() or "memory"
                value = str(meta.get("cortex_content") or body).strip()
            rows.append({
                "category": category or "saturn",
                "key": key or "memory",
                "value": value or body,
                "text": body,
                "has_saturn_meta": bool(category and key),
            })
        return {
            "text": str((data or {}).get("text") or "") if isinstance(data, dict) else "",
            "rows": rows,
            "tokens": int((data or {}).get("tokens") or 0) if isinstance(data, dict) else 0,
            "mode": str((data or {}).get("mode") or "") if isinstance(data, dict) else "",
        }

    def native_diagnostics(self, timeout: float = 1.5) -> dict:
        """Engine-side stats/metrics. Never starts Node; bounded and local."""
        if not self.is_ready():
            return {"ready": False, "reason": "backend not serving"}
        try:
            data = self._request("GET", "/diagnostics", timeout=timeout)
        except Exception as exc:
            return {"ready": False, "error": str(exc)}
        return data if isinstance(data, dict) else {"ready": False, "error": "unexpected payload"}

    def save_session(
        self,
        summary: str,
        language: str = "",
        max_sessions: int = 3,
        timeout: float | None = None,
    ) -> dict:
        """Append one session summary natively and keep the newest few.

        Authoritative: raises on failure so the caller never believes a session
        was stored when only a sidecar log says otherwise.
        """
        text = str(summary or "").strip()
        payload = self._session_payload({"summary": text, "language": language})
        if not payload["meta"]["saturn_value"]:
            raise SaturnWriteError("session summary is empty")
        self.require()
        result = self._request("POST", "/remember", payload, timeout=timeout or self.write_timeout)
        if not str((result or {}).get("id") or ""):
            raise SaturnWriteError("Saturn did not persist the session summary")

        grouped = self._existing(timeout=timeout or self.recall_timeout)
        sessions = [
            row for (category, _key), rows in grouped.items()
            if category == SESSION_CATEGORY
            for row in rows
        ]
        sessions.sort(key=lambda row: (row.get("updated") or "", row.get("at") or ""))
        keep = max(1, int(max_sessions or 1))
        for row in sessions[: max(0, len(sessions) - keep)]:
            self.delete_entry(str(row.get("id") or ""), timeout=timeout)
        return {
            "date": payload["meta"]["saturn_updated"],
            "summary": payload["meta"]["saturn_value"],
            "language": language,
            "id": str((result or {}).get("id") or ""),
        }

    def pop_session(self, timeout: float | None = None) -> dict | None:
        """Return AND consume the newest native session summary."""
        self.require()
        memory = self.load_memory(timeout=timeout, include_sessions=True)
        if not isinstance(memory, dict) or isinstance(memory, ProfileOutage):
            return None
        sessions = memory.get(SESSION_CATEGORY) or []
        if not sessions:
            return None
        newest = sessions[-1]
        grouped = self._existing(timeout=timeout or self.recall_timeout)
        for (category, key), rows in grouped.items():
            if category != SESSION_CATEGORY:
                continue
            if not rows:
                continue
            row = rows[-1]
            if row.get("value") != newest.get("summary"):
                continue
            if self.delete_entry(str(row.get("id") or ""), timeout=timeout):
                return newest
        return None

    def search_memory(self, query: str, limit: int = 8) -> str | None:
        try:
            entries = self.recall_entries(query, limit=limit)
        except Exception:
            return None
        rows = []
        for entry in entries:
            if entry["has_saturn_meta"]:
                rows.append(
                    f"{entry['category']}/{entry['key'].replace('_', ' ')}: {entry['value']}"
                )
            elif entry["text"]:
                rows.append(entry["text"])
        if not rows:
            return f"Nothing stored about '{query}'." if query else "I have not stored anything about this person yet."
        header = f"Stored facts matching '{query}':" if query else "Everything currently stored:"
        return header + "\n" + "\n".join(rows)

    def diagnostics(self) -> dict:
        """Local-only snapshot. Never starts Node and never exposes the key."""
        try:
            node = _node_binary()
        except Exception:
            node = None
        ready = self.is_ready()
        payload = {
            "enabled": _env_flag("SATURN_MEMORY_ENABLED", "1"),
            "backend_disabled": _backend_disabled(),
            "ready": ready,
            "url": self.base_url,
            "data_file": str(self.file),
            "shadow_write": self._shadow_status,
            "last_error": self._last_error,
            "node_available": bool(node),
            "ollama": self.ollama_status(),
        }
        # Only when the sidecar is already serving: diagnostics must not add
        # startup work to a health check.
        payload["native"] = self.native_diagnostics() if ready else {"ready": False, "reason": "backend not serving"}
        return payload

    def destination_identity(self, verify: bool = False, timeout: float | None = None) -> dict:
        """Which store this bridge is *actually* talking to.

        ``bridge.file`` and ``bridge.namespace`` describe what this process
        asked for; the sidecar's ``/diagnostics`` describes what is serving. A
        migration marker, a payload namespace or a cache is only meaningful for
        the pair, so both halves are reported here.

        The *store* half follows the sidecar: if it serves another file, that
        file is the destination and ``mismatch`` says so. The *namespace* half
        never does. ``/diagnostics`` reports the server's own default
        namespace, which is not the namespace a request asked for - the engine
        scopes every call by the namespace in the request/query - so adopting
        it would silently fold two bridges into one namespace.

        ``verify`` adds one ``/diagnostics`` read. A positive mismatch is
        *reported*, never raised: the caller decides to refuse. An unreachable
        or quiet sidecar is not a mismatch, only unverified - a diagnostic
        outage must not block a working store.
        """
        expected_file = str(self.file) if self.file else None
        expected_namespace = str(self.namespace or "").strip() or _namespace_id()
        reported_file = None
        reported_namespace = None
        verified = False
        mismatch = ""
        if verify:
            try:
                data = self._request(
                    "GET", "/diagnostics", timeout=timeout or self.recall_timeout
                )
            except Exception:
                data = None
            if isinstance(data, dict) and data.get("ok"):
                verified = True
                reported_file = str(data.get("file") or "") or None
                reported_namespace = str(data.get("namespace") or "").strip() or None
                if reported_file and expected_file and not _same_path(reported_file, expected_file):
                    mismatch = (
                        f"the serving sidecar reports store {reported_file!r} but this "
                        f"bridge expects {expected_file!r}"
                    )
        engine_file = reported_file or expected_file
        # The namespace is always the one this bridge asks for.
        ns = expected_namespace
        identity = hashlib.sha1(
            f"{engine_file or ''}\x1f{ns}".encode("utf-8")
        ).hexdigest()[:24]
        return {
            "engine_file": engine_file,
            "namespace": ns,
            "id": identity,
            "verified": verified,
            "mismatch": mismatch,
            "expected": {"engine_file": expected_file, "namespace": expected_namespace},
            "reported": {"engine_file": reported_file, "namespace": reported_namespace},
            # What the server calls its own default; informational only, never
            # used as the requested namespace.
            "served_namespace": reported_namespace,
        }

    def forget_identity(
        self,
        category: str,
        key: str,
        timeout: float | None = None,
        strict: bool = False,
    ) -> bool:
        """Delete every native row for one Saturn identity.

        ``strict`` raises on backend failure; the default keeps the historical
        boolean contract used by the mirror-era helper.
        """
        try:
            if strict:
                self.require()
            grouped = self._existing(timeout=timeout or self.recall_timeout)
        except SaturnError:
            raise
        except Exception as exc:
            if strict:
                raise SaturnWriteError(f"Saturn forget failed: {exc}") from exc
            return False
        rows = grouped.get((category, key), [])
        if not rows:
            return False
        deleted = False
        for row in rows:
            try:
                deleted = self.delete_entry(str(row.get("id") or ""), timeout=timeout) or deleted
            except Exception as exc:
                if strict:
                    raise SaturnWriteError(f"Saturn forget failed: {exc}") from exc
                return deleted
        return deleted

    def forget_key(self, category: str, key: str) -> bool:
        """Stage One alias kept for callers that only need a boolean."""
        return self.forget_identity(category, key)


_bridge: UniversalBridge | None = None
_bridge_lock = threading.Lock()


def get_bridge() -> UniversalBridge | None:
    global _bridge
    if _backend_disabled():
        return None
    if _bridge is None:
        with _bridge_lock:
            if _bridge is None:
                _bridge = UniversalBridge()
    return _bridge


def reset_bridge() -> None:
    """Drop the cached bridge, e.g. after the backend environment changed.

    Does not terminate a running sidecar; the next ``get_bridge()`` re-probes
    health and reuses the same local process.
    """
    global _bridge
    with _bridge_lock:
        _bridge = None
