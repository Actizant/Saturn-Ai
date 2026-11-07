"""
core/page_fetch.py
==================
GET one page with browser headers, under robots consent, and hand back text
that has already passed Saturn's integrity gate.

Three separate problems are solved here, and the order matters:

1. **Consent.** :func:`core.robots_consent.gate` runs before *every* request,
   including each redirect target, and nothing skips it. The browser headers
   below exist to get past bot-filtering firewalls on sites whose published
   policy allows access -- never past a site that said no. A refusal here is
   information, not an error to retry.
2. **Containment.** Fetched bodies are untrusted bytes. They are written to the
   system temp directory and a path resolving inside the repository or inside
   the user's Saturn data directory is refused, so a careers page can never be
   planted where Saturn would later read it as its own content.
3. **Integrity.** This is the ingress where the posting gate is applied
   automatically. The caller never has to remember to run
   ``job_posting_guard``: the only text this module returns is the gate's
   **cleaned** text, and what the gate removed is reported alongside it. Ask
   for the raw body and you get a path and an explicit flag, never a string
   that could be mistaken for safe reading material.

Properties, each pinned by tests/test_page_fetch.py:

  * GET only. The curl argv is fixed; there is no code path that sends a
    request body, a form, or a file, and the URL comes after ``--`` so it can
    never be parsed as an option. The urllib fallback (used only when curl is
    absent) has the same shape.
  * http(s) only, on the first hop and on every redirect.
  * bounded: 45 s overall, 5 redirects, 10 MB -- on both the curl and the
    urllib path, with the size cap applied after Content-Encoding is undone.
  * an HTTP status is never a failure. Non-2xx comes back as a result, because
    the status is what the caller escalates on, not what it crashes on.

Ported and adapted from the job-search web-research fetcher described in
``NOTICE`` (MIT); ``robots_consent.LICENSE`` carries the upstream copyright and
permission notice for both files. Upstream's ``tools/fetch_page.py`` printed a
tag-stripped body and left the integrity decision to the workflow that read it;
Saturn applies :mod:`core.posting_guard` here instead, which is why
``strip_tags`` no longer decides what the caller sees.

Usage:
    from core.page_fetch import fetch_url
    result = fetch_url("https://example.com/jobs/123")
    if result["ok"]:
        read_this = result["text"]        # gate-cleaned, never the raw body
"""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from core import posting_guard
from core.robots_consent import BROWSER, gate

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_LANGUAGE = "en-GB,en;q=0.9"
TIMEOUT_SECONDS = 45
MAX_REDIRECTS = 5
MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_CHARS = 6000

REDIRECT_CODES = (301, 302, 303, 307, 308)

# Only a hint for choosing prose rules, never a security decision: the gate
# itself decides whether a body looks like markup.
HTML_MARK = re.compile(r"<\s*(html|body|div|p)\b", re.I)


class RefusedByPolicy(Exception):
    """The site's published robots policy does not permit this path.

    Raised rather than returned so no caller can ignore it by forgetting to
    check a status code: there is nothing to read from a refused fetch.
    """

    def __init__(self, url: str, reason: str):
        super().__init__(f"robots policy does not permit {url}: {reason}")
        self.url = url
        self.reason = reason


class UnsafeOutput(Exception):
    """The requested output path would put untrusted bytes where Saturn reads
    its own files. Carries the reason, because the caller needs to say why."""


def build_curl_argv(url: str, out: Path, headers: Path, timeout: float) -> list:
    """One GET without automatic redirects, so the next hop can be gated."""
    return [
        "curl", "-sS",
        "--max-redirs", "0",
        "--max-time", str(timeout),
        "--max-filesize", str(MAX_BYTES),
        "--compressed",
        "-A", BROWSER,
        "-H", f"Accept: {ACCEPT}",
        "-H", f"Accept-Language: {ACCEPT_LANGUAGE}",
        "-D", str(headers),
        "-o", str(out),
        "-w", "%{http_code}",
        "--", url,
    ]


def read_location(header_text: str) -> Optional[str]:
    """Read Location from the final response (a proxy may add an earlier one)."""
    for block in reversed(re.split(r"\r?\n\r?\n", header_text)):
        if not block.startswith("HTTP/"):
            continue
        for line in block.splitlines():
            if line.lower().startswith("location:"):
                return line.partition(":")[2].strip()
        break
    return None


def read_content_type(header_text: str) -> str:
    """Content-Type of the final response, or "" when the server did not say."""
    for block in reversed(re.split(r"\r?\n\r?\n", header_text)):
        if not block.startswith("HTTP/"):
            continue
        for line in block.splitlines():
            if line.lower().startswith("content-type:"):
                return line.partition(":")[2].strip()
        break
    return ""


def media_type(content_type: Optional[str]) -> str:
    """Classify a body by what the server said it is: "json", "html" or "text".

    This decides *which* inspection a response gets, and that matters. The
    integrity gate is a prose defense: it removes markup a reader cannot see
    and folds whitespace a reader would not notice. Applied to a structured
    API record it is actively wrong, because its newline handling rewrites the
    text inside JSON strings -- a posting description loses its paragraph
    breaks, which is the only part of the record a reader cares about. So JSON
    is parsed and handed over whole; HTML and text are gated.
    """
    low = (content_type or "").split(";")[0].strip().lower()
    if low == "application/json" or low.endswith("+json"):
        return "json"
    if low in ("text/html", "application/xhtml+xml") or "html" in low:
        return "html"
    return "text"


def sniff_media(text: str) -> str:
    """Decide "json" from the body itself, for servers that omit the header.

    Only a body that actually parses counts: a page that merely starts with a
    brace is not JSON, and guessing wrong would route real prose away from the
    gate. That is why the check is a parse and not a prefix test.
    """
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return "text"
    try:
        json.loads(text)
    except Exception:  # noqa: BLE001 -- unparseable means it is not JSON
        return "text"
    return "json"


def parse_json(text: str, limit: int) -> tuple[Any, str, bool]:
    """(value, excerpt, truncated) for a body already known to be JSON.

    ``excerpt`` is a re-serialised, capped rendering used only for display:
    ``value`` is the parsed object, untouched, and is what a caller iterates.
    """
    value = json.loads(text)
    rendered = json.dumps(value, ensure_ascii=False)
    return value, rendered[:limit], len(rendered) > limit


def fetch_with_curl(url: str, out: Path, timeout: float) -> tuple[int, Optional[str], str]:
    fd, header_name = tempfile.mkstemp(prefix=".page_fetch_headers.")
    os.close(fd)  # curl must be able to open it on Windows too
    headers = Path(header_name)
    try:
        # No capture_output on this box: its pythoncore 3.14 reader thread
        # dies on any non-ASCII byte in a pipe and communicate() returns None
        # -- an ASCII curl error would corrupt the status code, a non-ASCII
        # one would erase it. The body already goes to a file via -o; stdout
        # carries only the -w digits, and stderr is dropped to DEVNULL
        # because curl exit codes already say what went wrong.
        proc = subprocess.run(
            build_curl_argv(url, out, headers, timeout),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout + 15,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"curl exit {proc.returncode}")
        raw = proc.stdout or b""
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="replace")
        code = int(raw.strip() or 0)
        header_text = headers.read_text(encoding="iso-8859-1")
        location = read_location(header_text)
        # Normalised here, so both transports hand the caller the same three
        # values in the same shape and fetch() never sees a raw header string.
        media = media_type(read_content_type(header_text))
    finally:
        headers.unlink(missing_ok=True)
    if out.stat().st_size > MAX_BYTES:
        raise RuntimeError(f"response exceeds {MAX_BYTES} bytes after decompression")
    return code, location, media


class _NoRedirects(HTTPRedirectHandler):
    """Return the 3xx response to the caller for a robots check first."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def fetch_with_urllib(url: str, out: Path, timeout: float) -> tuple[int, Optional[str], str]:
    """Fallback for machines without curl. One GET, gzip/deflate only."""
    request = Request(url, headers={
        "User-Agent": BROWSER,
        "Accept": ACCEPT,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Accept-Encoding": "gzip, deflate",
    })
    try:
        response = build_opener(_NoRedirects()).open(request, timeout=timeout)  # noqa: S310
    except HTTPError as exc:
        response = exc
    with response:
        code = response.status if hasattr(response, "status") else response.code
        location = response.headers.get("Location")
        media = media_type(response.headers.get("Content-Type"))
        if code in REDIRECT_CODES and location:
            return code, location, media
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise RuntimeError(f"response exceeds {MAX_BYTES} bytes")
    if encoding == "gzip":
        body = gzip.decompress(body)
    elif encoding == "deflate":
        body = zlib.decompress(body, -zlib.MAX_WBITS)
    if len(body) > MAX_BYTES:
        raise RuntimeError(f"response exceeds {MAX_BYTES} bytes after decompression")
    out.write_bytes(body)
    return code, location, media


def strip_tags(markup: str, limit: int) -> str:
    """Plain text from HTML: drop script/style/noscript/svg and comments, strip
    tags, unescape entities, fold whitespace. Modern sites embed copy inside
    JSON blobs, so stray attribute fragments around real text are normal.

    Kept because it is the upstream body-text extractor and its behaviour is
    pinned by the equivalence test. It is **not** what :func:`fetch` returns,
    and the difference is a security property rather than a preference: this
    function deletes tags but keeps the text between them, so the contents of
    ``<span style="display:none">`` come out as innocent-looking prose. It is
    not a defense and was never meant to be one; the integrity gate removes
    that content *and reports it*, which is why the ingress uses the gate.
    """
    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", markup)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


def default_out(url: str) -> Path:
    """A temp path keyed by the URL, outside anything Saturn reads as its own."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"saturn_fetch_page_{digest}.html"


def _protected_roots() -> list[Path]:
    """Directories a fetched body must never land in."""
    roots = [REPO_ROOT]
    try:
        from core.user_paths import get_user_data_dir
        roots.append(get_user_data_dir())
    except Exception:  # noqa: BLE001 -- a missing data dir is not a reason to write into the repo
        pass
    return roots


def assert_safe_output(path: Path) -> None:
    """Refuse any output path inside the repo or the Saturn data directory."""
    resolved = Path(path).resolve()
    for root in _protected_roots():
        try:
            resolved.relative_to(Path(root).resolve())
        except ValueError:
            continue
        raise UnsafeOutput(
            f"refusing to write fetched content inside {root} -- a saved page is "
            "untrusted input and belongs in the temp directory"
        )


def _decode(raw: bytes) -> str:
    return posting_guard.decode_page(raw)


def gate_body(html_text: str, max_chars: int) -> Dict[str, Any]:
    """Run the integrity gate over a fetched body and shape it for the caller.

    ``text`` is the gate's cleaned output, capped at ``max_chars``; everything
    the gate removed is in ``findings``, and ``suspicious`` is the signal that
    the body was written to be read by an agent rather than a person.
    """
    report = posting_guard.guard_text(html_text, "auto")
    cleaned = report.get("cleaned") or ""
    return {
        "verdict": report.get("verdict", ""),
        "suspicious": bool(report.get("suspicious")),
        "text": cleaned[:max_chars],
        "text_characters": len(cleaned),
        "truncated": len(cleaned) > max_chars,
        "findings": report.get("findings", {}),
    }


def raw_body(result: Dict[str, Any], limit: int = 200_000) -> str:
    """Read the untouched saved body back, deliberately and with effort.

    This exists so the raw page is *reachable* -- debugging a verdict sometimes
    means looking at exactly what was removed -- while never being the default
    thing a caller receives. Anything handed to a model should come from
    ``result["gate"]["text"]`` instead.
    """
    path = result.get("path")
    if not path:
        raise ValueError("nothing was saved, so there is no raw body to read")
    return _decode(Path(path).read_bytes())[:limit]


def fetch(url: str, out: Optional[Path] = None, timeout: float = TIMEOUT_SECONDS,
          max_chars: int = DEFAULT_MAX_CHARS, opener: Optional[Any] = None,
          consent: Optional[Any] = None) -> Dict[str, Any]:
    """Fetch one URL under robots consent and return gate-cleaned text.

    ``opener`` and ``consent`` exist so tests can replace the transport and the
    policy read without touching the network; production callers leave them
    alone and get the real curl/urllib path with the real gate.

    Returns ``{"ok", "code", "url", "path", "size", "consent", "media"}`` plus
    exactly one readable payload: ``"gate"`` for prose and HTML, where
    ``gate["text"]`` is the only text worth reading, or ``"data"`` for a
    structured API response, parsed whole. Raises :class:`RefusedByPolicy` when
    the site's policy forbids the path, and :class:`UnsafeOutput` when the body
    would be written inside the repo or the Saturn data directory. Any
    transport failure propagates as-is; the caller decides what a 404 or a
    timeout means.
    """
    send = opener or (fetch_with_curl if shutil.which("curl") else fetch_with_urllib)
    check = consent or gate

    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"page_fetch only fetches http(s), got scheme {scheme!r}")

    destination = Path(out) if out is not None else default_out(url)
    assert_safe_output(destination)

    deadline = time.monotonic() + timeout
    current = url
    consent_lines: list[str] = []
    code: Optional[int] = None
    declared_media = ""

    for redirect in range(MAX_REDIRECTS + 1):
        target_scheme = urlsplit(current).scheme.lower()
        if target_scheme not in ("http", "https"):
            raise ValueError(f"redirect target must be http(s), got scheme {target_scheme!r}")

        rc, message = check(current)
        consent_lines.append(message)
        if rc != 0:
            raise RefusedByPolicy(current, message)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("fetch exceeded the time limit")

        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, body_name = tempfile.mkstemp(dir=str(destination.parent),
                                         prefix=".page_fetch.", suffix=".tmp")
        os.close(fd)  # curl/urllib may reopen the path on Windows
        body = Path(body_name)
        try:
            got, location, media = send(current, body, remaining)
            if got in REDIRECT_CODES and location:
                if redirect == MAX_REDIRECTS:
                    raise RuntimeError(f"more than {MAX_REDIRECTS} redirects")
                # Encode the header's raw bytes before resolving; preserve Unicode in the base URL.
                current = urljoin(current, quote(location, encoding="iso-8859-1",
                                                 safe=string.punctuation))
                continue
            os.replace(body, destination)
            code = got
            declared_media = media
            break
        finally:
            body.unlink(missing_ok=True)

    raw_bytes = destination.read_bytes()
    markup = _decode(raw_bytes)
    result: Dict[str, Any] = {
        "ok": True,
        "code": code,
        "url": current,
        "path": str(destination),
        "size": len(raw_bytes),
        "consent": "\n".join(consent_lines),
    }

    # A structured record is parsed and handed over whole; prose is gated and
    # only its cleaned form is handed over. Both choices are deliberate, and
    # sniff_media() only calls something JSON when the header said so or the
    # body actually parses, so real prose can never be routed away from the
    # gate by starting with a brace.
    media = declared_media or media_from_body(markup)
    result["media"] = media
    if media == "json":
        try:
            value, excerpt, truncated = parse_json(markup, max_chars)
        except (ValueError, RecursionError) as exc:
            # Claimed JSON that will not parse: fall back to the prose gate
            # rather than hand over an ungated body under any name.
            result["media"] = media = "text"
            result["json_error"] = f"{type(exc).__name__}: {exc}"
        else:
            result["data"] = value
            result["data_excerpt"] = excerpt
            result["truncated"] = truncated
            result["gate"] = None

    if media != "json":
        result["gate"] = gate_body(markup, max_chars)
    return result


def media_from_body(text: str) -> str:
    """The declared type when the server gave one, else what the body proves."""
    sniffed = sniff_media(text)
    return sniffed if sniffed == "json" else "html" if HTML_MARK.search(text) else "text"


def fetch_url(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> Dict[str, Any]:
    """The voice- and planner-facing entry point.

    Never raises for a refusal or a bad page: everything comes back as a result
    dict with ``ok`` and a human-readable ``error``, because the caller is a
    conversation, not a build script.
    """
    try:
        return fetch(url, max_chars=max_chars)
    except RefusedByPolicy as exc:
        return {"ok": False, "error": str(exc), "refused_by_policy": True,
                "hint": ("Search for the employer's own posting instead. This is a "
                         "consent decision, not a network fault, so retrying will not help.")}
    except UnsafeOutput as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- transport failures of any shape
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
