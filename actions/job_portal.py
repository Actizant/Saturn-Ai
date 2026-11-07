"""
actions/job_portal.py
=====================
The voice- and agent-facing surface for discovering open roles on an
employer's own public applicant-tracking board.

This is the *discovery* half of the job-search workflow. It asks one board for
its list of postings and reports what came back. The engine it leans on is
already built and proven:

  * :func:`core.page_fetch.fetch_url` runs the robots consent gate before every
    request and hands back a parsed body, so nothing here reaches a board the
    publisher declined.
  * :func:`core.job_key.make_key` gives each posting one stable identity, the
    same key the tracking phase will later use, so a listing found here is the
    listing tracked later rather than a near-duplicate.

Only two boards are advertised, and that list is deliberately short: Greenhouse
and Lever each expose a keyless, JSON, public postings endpoint that the consent
gate permits. Every other ATS is refused by name rather than guessed at. The one
verb that touches anything is ``track``, and it only writes a line to Saturn's
own local job notes through :mod:`core.job_key` -- it never opens a board's
application form. "draft" and "apply" stay unimplemented; this module lists
roles and remembers them, it does not submit an application.

Reachable from speech and from the planner as the ``job_portal`` tool.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# The verbs this surface answers. "search" lists a board; "portals" says which
# boards exist and how to name one; "track" saves or recalls a role in Saturn's
# own local job notes. Anything else is refused by name so the caller learns
# what is missing instead of getting a vague failure.
VERBS = ("search", "portals", "track")

# Later phases of the job-search workflow that no tool honours yet. These stay
# unbuilt and the refusal names them, so the honest surface never implies this
# tool drafts material or submits an application. "track", by contrast, is now
# built here (a purely local save), and "check" belongs to job_posting_guard.
_FUTURE_VERBS = {
    "draft": "drafting a CV or cover letter",
    "apply": "submitting an application",
}
_REDIR_VERBS = {
    "check": "job_posting_guard",
}

DEFAULT_LIMIT = 15


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _first_string(job: Dict[str, Any], keys) -> str:
    """The first of ``keys`` that is a non-empty string somewhere on the job.

    Greenhouse and Lever name the same idea differently and a location can be a
    bare string or a ``{"name": ...}`` object, so a field is read through this
    rather than indexed blindly -- a surprise key shape must degrade to "", not
    raise, and never to an invented value.
    """
    for key in keys:
        value = job.get(key)
        if isinstance(value, dict):
            # Greenhouse nests a location as {"name": ...}; Lever nests it as
            # categories {"location": ...}. Try each known inner name.
            value = next((value.get(inner) for inner in ("name", "text", "location")
                          if isinstance(value.get(inner), str) and value.get(inner).strip()),
                         None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# The two public board shapes, named exactly as a caller must name them. Each
# says how to build the request URL and where the list of postings lives inside
# the parsed body, so the tool never has to guess a portal's shape at runtime.
PORTALS: Dict[str, Dict[str, Any]] = {
    "greenhouse": {
        "url": lambda token: f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        # Greenhouse wraps its postings under "jobs"; the top level is a dict.
        "jobs": lambda data: data.get("jobs") if isinstance(data, dict) else None,
        "title": ("title",),
        "location": ("location",),
        "url_field": ("absolute_url", "url"),
    },
    "lever": {
        "url": lambda token: f"https://api.lever.co/v0/postings/{token}?mode=json",
        # Lever returns the postings as the top-level JSON list itself.
        "jobs": lambda data: data if isinstance(data, list) else None,
        "title": ("text", "title"),
        "location": ("categories", "location"),
        "url_field": ("hostedUrl", "detailUrl", "applyUrl", "url"),
    },
}


def _refuse(action: str) -> str:
    if action in _REDIR_VERBS:
        return (
            f"'{action}' is not what job_portal does - that is the "
            f"'{_REDIR_VERBS[action]}' tool, Saturn's posting integrity gate. "
            f"job_portal answers to: {', '.join(VERBS)}."
        )
    if action in _FUTURE_VERBS:
        return (
            f"job_portal does not do {_FUTURE_VERBS[action]} yet - that is a later "
            f"phase of the job-search workflow. It answers to: {', '.join(VERBS)}."
        )
    return f"Unknown job_portal action '{action}'. It answers to: {', '.join(VERBS)}."


def _describe_portals() -> str:
    lines = [
        "job_portal searches an employer's own public applicant-tracking board.",
        "Boards it knows (each exposes a keyless public postings list):",
    ]
    for name, spec in PORTALS.items():
        sample = spec["url"]("acme")
        lines.append(f"  - {name}: {sample}")
    lines.append(
        "Pass the company's board token as 'company' (the slug in a careers URL, "
        "e.g. greenhouse.io/boards/<token> or lever.co/postings/<token>)."
    )
    lines.append(
        "Any other board is refused: this surface only advertises what the robots "
        "consent gate has been shown to permit, never a portal it has not proven."
    )
    lines.append(
        "job_portal lists roles and can track one in Saturn's own local notes; "
        "it never drafts material or submits an application. Run "
        "job_posting_guard over any posting text before reasoning about it."
    )
    return "\n".join(lines)


def _report(company: str, portal: str, jobs: List[Dict[str, Any]],
            spec: Dict[str, Any], shown: int, total: int, matched: int) -> str:
    """Turn a filtered posting list into one honest block of text."""
    from core import job_key

    lines = [f"{total} open role(s) on the {portal} board for {company}."]
    if matched != total:
        lines.append(
            f"{matched} matched the filter; showing {min(shown, matched)} of them.")
    elif shown < total:
        lines.append(f"Showing the first {shown} of {total}.")

    for job in jobs[:shown]:
        title = _first_string(job, spec["title"]) or "(untitled)"
        location = _first_string(job, spec["location"]) or "location not stated"
        link = _first_string(job, spec["url_field"])
        key = job_key.make_key(company, title, link)
        entry = f"  - {title} @ {location}"
        if link:
            entry += f"\n      {link}"
        entry += f"\n      key: {key}"
        lines.append(entry)

    lines.append(
        "These are titles and links the board published; this search has not read "
        "any posting's full text, applied, or saved anything. To keep one of these "
        "roles in Saturn's local notes, use action:'track'. Gate a posting with "
        "job_posting_guard before treating its contents as trustworthy."
    )
    return "\n".join(lines)


def _track(params: Dict[str, Any]) -> str:
    """Remember or recall one role in Saturn's own local job notes.

    This is the only verb here that writes anything, and what it writes is a
    private note on the user's machine through :mod:`core.job_key` -- never an
    application, never a request to a board. Because the key comes from the same
    :func:`core.job_key.make_key` the search report prints, a tracked role is the
    role that was listed, not a near-duplicate of it.
    """
    import datetime

    from core import job_key

    company = _clean(params.get("company"))
    title = _clean(params.get("title") or params.get("role"))
    url = _clean(params.get("url") or params.get("link"))
    mode = _clean(params.get("mode") or params.get("what")).lower()
    if not mode:
        mode = "record" if (company and title) else "list"

    if mode == "record":
        if not (company and title):
            return (
                "job_portal needs a company and a title to track one role "
                "(take them from a search result). Nothing was saved. Use "
                "mode:'list' to see what is already tracked."
            )
        entry = {
            "company": company,
            "title": title,
            "url": url,
            "tracked_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        key = job_key.record_seen(entry)
        return (
            f"Tracking saved locally: {title} @ {company} (key {key}). Saturn "
            f"added one line to its own job notes at {job_key.state_path()}; it "
            f"did not apply, contact anyone, or read the posting's text. Run "
            f"job_posting_guard over a posting before reasoning about it."
        )

    if mode in ("list", "show", "status"):
        seen = job_key.load_seen()
        if not seen:
            return (
                "Nothing is tracked yet. Track a role from a search result with "
                "action:'track', mode:'record' and the posting's company and title."
            )
        lines = [f"{len(seen)} role(s) tracked locally:"]
        for key, entry in sorted(seen.items()):
            c = _clean(entry.get("company")) or "(no company)"
            t = _clean(entry.get("title")) or "(no title)"
            lines.append(f"  - {t} @ {c}")
        lines.append(
            "These are Saturn's own saved notes, not a status from any board and "
            "not applications; nothing was submitted or sent elsewhere."
        )
        return "\n".join(lines)

    return (
        f"job_portal track does not know mode '{mode}'. It records a role "
        f"(mode:'record' with a company and a title) or lists the tracked ones "
        f"(mode:'list')."
    )


def job_portal(parameters: Optional[Dict[str, Any]] = None,
               player: Any = None,
               response: Any = None) -> str:
    """Search a public ATS board and list its open roles. Returns a text report."""
    from core import page_fetch

    params = dict(parameters or {})
    action = _clean(params.get("action") or "search").lower()

    if action == "portals":
        return _describe_portals()

    if action == "track":
        return _track(params)

    if action != "search":
        return _refuse(action)

    portal = _clean(params.get("portal") or params.get("provider")).lower()
    company = _clean(params.get("company") or params.get("token") or params.get("board"))
    if portal not in PORTALS:
        known = ", ".join(PORTALS)
        if portal:
            return (
                f"job_portal does not search '{portal}' boards - it is not a board "
                f"Saturn has proven the consent gate permits. It searches: {known}. "
                f"Ask action:'portals' for the details."
            )
        return f"job_portal needs a board to search. Choose one of: {known}."
    if not company:
        return (
            f"job_portal needs the company's {portal} board token (the slug in "
            f"their careers URL). Nothing was fetched."
        )

    spec = PORTALS[portal]
    url = spec["url"](company)
    result = page_fetch.fetch_url(url)

    if not result.get("ok"):
        if result.get("refused_by_policy"):
            return (
                f"job_portal did not fetch the {portal} board for {company}: the "
                f"robots policy there does not permit it ({result.get('error')}). "
                f"This is a consent decision, not a network fault, so retrying "
                f"will not help. Search for the employer's own posting instead."
            )
        return (
            f"job_portal could not reach the {portal} board for {company}: "
            f"{result.get('error', 'no reason given')}."
        )

    data = result.get("data")
    jobs = spec["jobs"](data)
    if jobs is None:
        return (
            f"job_portal fetched {url} but the {portal} board did not return a "
            f"list of postings (media {result.get('media')}). Nothing is claimed "
            f"about roles that were not in the response."
        )

    total = len(jobs)
    query = _clean(params.get("query") or params.get("keyword")).lower()
    location = _clean(params.get("location")).lower()
    if query or location:
        def keep(job):
            title = _first_string(job, spec["title"]).lower()
            loc = _first_string(job, spec["location"]).lower()
            if query and query not in title and query not in loc:
                return False
            if location and location not in loc:
                return False
            return True
        jobs = [j for j in jobs if keep(j)]

    matched = len(jobs)
    if not jobs:
        note = " after the filter" if (query or location) else ""
        return (
            f"The {portal} board for {company} listed no open roles{note}. "
            f"That is the board's own answer, not a network fault."
        )

    try:
        limit = int(params.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, 100))

    text = _report(company, portal, jobs, spec, limit, total, matched)

    if player is not None and hasattr(player, "write_log"):
        try:
            player.write_log(f"[job_portal] {portal}/{company}: {matched} of {total} postings")
        except Exception:  # noqa: BLE001 -- logging must never eat the answer
            pass
    return text
