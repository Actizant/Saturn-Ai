"""
core/job_key.py
===============
The canonical dedup key for one job posting, and an audit of the key file.

Tracking only works if one posting is one entry. Keyed by hand, "company +
title" gets slugified a little differently every run, and the state file
accumulates both failures at once:

  * keys carrying characters that break things downstream -- a ``/`` in a title
    splits the archive folder the key is used for across directories
    (``deloitte_junior-cybersecurity-analyst-(ot/iot)``), a ``,`` in a company
    name does similar damage (``ops-consulting,-llc_malware-analyst``)
  * the same job stored twice, because one run truncated the title at a
    different point from the next -- two keys, one posting, one URL, and dedup
    was the entire point of the file

Both go away by making the key a pure, deterministic function of the posting.
Truncation is length-capped *and* disambiguated by a hash of the full slug, so a
long title always produces the same key and two different long titles never
collide.

A title that slugifies to nothing (a posting in a non-Latin script) has no
usable key half at all -- ``securion_`` was a real entry, and it would have
collided with every future non-Latin posting from that company. Those fall back
to the portal's numeric id from the URL. A title or company that slugifies to
*less* than it says is the same problem one step on: "Программист 1С" and
"Аналитик 1С" both fold to ``1``, "Сбер AI" and "Яндекс AI" both fold to ``ai``,
so whenever the ASCII fold drops letters the surviving fragment is only a
readable prefix and the identity comes from the same fallback.

Saturn's adaptation is the location of the state: upstream kept it in the
repository at ``job_scraper/seen_jobs.json``. Saturn's job state is personal
data, so it lives in the user's data directory beside everything else about
them and never in the installed tree. The key rule itself is upstream's, byte
for byte, and :mod:`tests.test_job_key` proves the port against the original
implementation over the counterexamples above.

Usage:
    from core.job_key import make_key, load_seen, record_seen
    key = make_key("Acme Corp", "SOC Analyst (L2)", url)
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional

COMPANY_MAX = 40
TITLE_MAX = 60
HASH_LEN = 6

# Anything outside this set becomes a separator. Deliberately strict: "/" and
# "," are the characters that actually caused damage, and an allowlist cannot
# be surprised by the next punctuation mark a job board invents.
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_JOB_ID = re.compile(r"(\d{6,})")


def state_path() -> Path:
    """Where Saturn keeps the postings it has already seen.

    Personal state, so it belongs in the user's data directory, not in the
    installed application -- the opposite of upstream, where it sat in the repo.
    """
    from core.user_paths import get_user_data_dir

    return get_user_data_dir() / "job_search" / "seen_jobs.json"


def slugify(text: str) -> str:
    """Lowercase ASCII slug. Non-Latin scripts legitimately reduce to ''."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG.sub("-", ascii_only.lower()).strip("-")


def _lost_letters(text: str) -> bool:
    """True when the ASCII fold dropped non-Latin letters, so the slug under-identifies.

    NFKD turns "ü" into "u" plus a combining mark and "ﬁ" into "fi", so most
    Latin text keeps every letter. A Cyrillic, Greek, CJK or Arabic letter has
    no ASCII decomposition and vanishes; a mixed-script name then keeps only
    its Latin or digit fragment, and two different names can share it.

    Latin letters that also lack a decomposition ("ø", "æ", "ß", "ł") are
    deliberately NOT counted: "Ørsted" has keyed as "rsted" since the rule
    existed, and treating it as lossy would re-key every Danish company in a
    live seen_jobs.json for a collision that does not happen in practice.
    """
    if not text:
        return False
    for ch in unicodedata.normalize("NFKD", str(text)):
        if ord(ch) < 128 or not ch.isalpha():
            continue
        if not unicodedata.name(ch, "LATIN").startswith("LATIN"):
            return True
    return False


def _cap(slug: str, limit: int) -> str:
    """Cap length without making truncation lossy across runs.

    A bare truncation is what produced the duplicate Deloitte entries: two runs
    cut the same title at different points and the file gained a second key for
    one job. Appending a hash of the *full* slug makes the result deterministic
    for a given title and distinct for any other.
    """
    if len(slug) <= limit:
        return slug
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:HASH_LEN]
    return f"{slug[:limit].rstrip('-')}-{digest}"


def make_key(company: str, title: str, url: str = "") -> str:
    """The canonical seen_jobs.json key for one posting."""
    company_slug = _cap(slugify(company), COMPANY_MAX)
    if not company_slug or _lost_letters(company):
        name = unicodedata.normalize("NFC", str(company or "").strip().casefold())
        # An absent name stays unknown; a non-Latin name still has an identity.
        # A mixed-script name keeps its Latin fragment as a readable prefix, but
        # the identity is the hash: "Сбер AI" and "Яндекс AI" both fold to "ai".
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:HASH_LEN]
        if not name:
            company_slug = "unknown-company"
        else:
            company_slug = f"{company_slug}-{digest}" if company_slug else f"company-{digest}"
    title_slug = _cap(slugify(title), TITLE_MAX)
    if not title_slug or _lost_letters(title):
        # No Latin characters in the title, or not enough of them to identify
        # it: "Программист 1С" and "Аналитик 1С" both fold to "1". The portal's
        # own numeric id is the stable handle; a surviving fragment stays as a
        # readable prefix only. Never emit a bare "company_" prefix.
        fragment = title_slug
        match = _JOB_ID.search(url or "")
        if match:
            title_slug = f"{fragment}-{match.group(1)}" if fragment else match.group(1)
        else:
            basis = slugify(unicodedata.normalize("NFKD", str(title or url or "")))
            # Hash the URL alone when there is one. The URL is the posting's
            # identity; the title is not. Including the title made the key
            # change whenever a portal re-listed the same posting with the
            # title altered, which stores one job twice - the failure this
            # whole helper exists to prevent. Portals whose ids carry no run
            # of six digits never reach the branch above, so for them this
            # hash is the only key half there is: freehire's real slugs look
            # like "inzhener-ooo-chen-hlk3qjfg", and its Cyrillic and Greek
            # titles slugify to nothing. With no URL, the title is all that
            # is left to key on.
            digest_basis = str(url) if url else str(title)
            digest = hashlib.sha1(digest_basis.encode("utf-8")).hexdigest()[:HASH_LEN]
            # A lossy fragment needs the digest to tell two postings apart; an
            # empty title keeps the bare basis (a URL-derived slug, or
            # "untitled-<hash>") exactly as before the fragment rule existed.
            title_slug = f"{fragment}-{digest}" if fragment else (basis or f"untitled-{digest}")
    return f"{company_slug}_{title_slug}"


# A canonical key is "<company-slug>_<title-slug>": lowercase alphanumerics and
# hyphens on either side of exactly one underscore. The underscore is the
# separator, so it is the one character outside the slug alphabet that belongs.
_CANONICAL = re.compile(r"^[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9-]*$")


def is_canonical(key: str) -> bool:
    """Structurally safe as a dedup key and as an archive folder name."""
    return bool(key) and bool(_CANONICAL.match(key))


def is_legacy_shape(key: str) -> bool:
    """Old three-part "company_title_location" keys.

    Harmless - they carry no path-breaking character - but they are not what
    make_key produces, so a later run would store the same job under a new key
    and reintroduce a duplicate. Reported apart from real damage so the fix
    stays a decision rather than an automatic rename.
    """
    return bool(key) and key.count("_") > 1 and all(
        re.fullmatch(r"[a-z0-9][a-z0-9-]*", part) for part in key.split("_") if part
    )


def audit(path: Optional[Path] = None) -> Dict[str, Any]:
    """Describe the damage in a key file without changing it.

    Returns a dict rather than printing and exiting, so the answer can be read
    aloud. ``malformed_keys`` and ``duplicate_urls`` are damage;
    ``keys_not_matching_current_rule`` is drift, reported apart because
    renaming those is a decision the user makes, never an automatic rewrite of
    their history.
    """
    target = Path(path) if path is not None else state_path()
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"cannot read {target}: {exc}", "path": str(target)}
    seen = doc.get("seen", doc) if isinstance(doc, dict) else doc
    if not isinstance(seen, dict):
        return {"ok": False, "error": f"{target}: expected an object of job entries",
                "path": str(target)}
    not_objects = [k for k, v in seen.items() if not isinstance(v, dict)]
    if not_objects:
        return {"ok": False, "error": f"entry is not an object: {not_objects[0]}",
                "path": str(target)}
    bad_url = [k for k, v in seen.items()
               if v.get("url") is not None and not isinstance(v["url"], str)]
    if bad_url:
        return {"ok": False, "error": f"entry url is not a string: {bad_url[0]}",
                "path": str(target)}

    malformed = [k for k in seen if not is_canonical(k) and not is_legacy_shape(k)]
    legacy = [k for k in seen if is_legacy_shape(k)]
    by_url: Dict[str, list] = {}
    for key, entry in seen.items():
        url = (entry.get("url") or "").rstrip("/")
        if url:
            by_url.setdefault(url, []).append(key)
    duplicates = {u: ks for u, ks in by_url.items() if len(ks) > 1}
    # A key that does not match what make_key would produce today is drift, not
    # damage: reported separately so a rename is a choice, never automatic.
    drift = [
        k for k, v in seen.items()
        if is_canonical(k) and k != make_key(v.get("company", ""), v.get("title", ""), v.get("url", ""))
    ]
    return {
        "ok": True,
        "path": str(target),
        "entries": len(seen),
        "malformed_keys": malformed,
        "legacy_three_part_keys": legacy,
        "duplicate_urls": duplicates,
        "keys_not_matching_current_rule": drift,
        "clean": not (malformed or duplicates),
    }


def load_seen(path: Optional[Path] = None) -> Dict[str, Any]:
    """The stored entries, or {} when there is no file yet.

    An unreadable file raises rather than returning empty: silently starting a
    new key file would look exactly like having never seen these jobs before,
    and the next run would re-apply to all of them.
    """
    target = Path(path) if path is not None else state_path()
    if not target.exists():
        return {}
    doc = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{target}: expected an object of job entries")
    seen = doc.get("seen", doc)
    if not isinstance(seen, dict):
        raise ValueError(f"{target}: expected an object of job entries")
    return seen


def record_seen(entry: Dict[str, Any], path: Optional[Path] = None) -> str:
    """Store one posting under its canonical key and return that key.

    The caller must supply ``company`` and ``title``; the key is computed here
    and nowhere else, so a run that invents its own slug cannot create a second
    entry for a job that is already recorded.
    """
    key = make_key(entry.get("company", ""), entry.get("title", ""), entry.get("url", ""))
    if not is_canonical(key):
        # Unreachable for real input, but a key that cannot be a folder name
        # must never be written: it would corrupt the file for every later run.
        raise ValueError(f"refusing to store a non-canonical key: {key!r}")
    target = Path(path) if path is not None else state_path()
    stored = load_seen(target)
    stored[key] = entry
    target.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps({"seen": stored}, indent=2, ensure_ascii=False)
    target.write_text(document, encoding="utf-8")
    return key
