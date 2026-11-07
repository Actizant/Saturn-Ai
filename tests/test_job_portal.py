"""
tests/test_job_portal.py
========================
actions/job_portal.py discovers open roles on an employer's public ATS board,
and the discovery only means anything if what it claims is exactly what the
board returned.

Two things are checked, and the second matters most:

1. The tool turns a board response into an honest report -- including the cases
   where there is nothing to report: a board the consent gate refused, an empty
   board, a filter that matches nothing, a portal it does not advertise. In
   every one of those it must say what is missing rather than invent a role.
2. The three vocabularies agree. The tool answers some set of verbs, main.py
   advertises a set to the model and agent/planner.py a set to the agent loop;
   if they ever drift apart Saturn is claiming a verb it cannot honour. This is
   the same guard job_posting_guard is held to.

Offline tests feed canned board shapes through the ingress by monkeypatching
:func:`core.page_fetch.fetch_url`, so no request leaves the machine. The one
exception is the live probe at the bottom, which is deliberately run by default:
it is the standing proof that the discovery path the whole surface rests on is
real, not a fixture. It asks one Greenhouse board and fails loudly if the
consent-gated ingress cannot turn it into at least one listed posting.
"""

import re
from pathlib import Path

import pytest

from actions.job_portal import PORTALS, VERBS, job_portal
from core import page_fetch

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Reading source rather than importing it (the vocabulary drift guard)
# --------------------------------------------------------------------------- #

def _source(relative):
    return (ROOT / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _schema_verbs():
    """The verbs main.py's job_portal declaration advertises to the model."""
    block = _source("main.py").split('"name": "job_portal"', 1)[-1]
    match = re.search(r'"action":\s*\{[^{}]*"description":\s*"([^"]+)"', block)
    assert match, "no action description found in the job_portal schema"
    return {part.strip() for part in match.group(1).split("|")}


def _planner_verbs():
    """The verbs agent/planner.py tells the agent loop to emit for job_portal."""
    block = _source("agent/planner.py").split("\njob_portal\n", 1)[-1]
    match = re.search(r"^\s*action:.*$", block, re.MULTILINE)
    assert match, "the planner's job_portal block lost its action line"
    return set(re.findall(r'"([a-z_]+)"', match.group(0)))


# --------------------------------------------------------------------------- #
# Canned board shapes, patched over the real ingress
# --------------------------------------------------------------------------- #

GREENHOUSE_BODY = {
    "ok": True, "code": 200, "media": "json", "consent": "ALLOWED",
    "data": {"jobs": [
        {"title": "Senior Data Engineer", "location": {"name": "Remote, US"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/111111"},
        {"title": "Abuse Investigator", "location": {"name": "Dublin"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/222222"},
    ]},
}

LEVER_BODY = {
    "ok": True, "code": 200, "media": "json", "consent": "ALLOWED",
    "data": [
        {"text": "Frontend Engineer", "categories": {"location": "London"},
         "hostedUrl": "https://jobs.lever.co/acme/aaa"},
    ],
}


def patch_board(monkeypatch, payload):
    """Replace the ingress so it hands back ``payload`` and records the URL."""
    seen = []

    def fake_fetch_url(url, **kwargs):
        seen.append(url)
        return payload

    monkeypatch.setattr(page_fetch, "fetch_url", fake_fetch_url)
    return seen


# --------------------------------------------------------------------------- #
# A real board becomes an honest list
# --------------------------------------------------------------------------- #

def test_greenhouse_board_lists_titles_locations_and_links(monkeypatch):
    patch_board(monkeypatch, GREENHOUSE_BODY)
    answer = job_portal({"action": "search", "portal": "greenhouse", "company": "acme"})
    assert "2 open role(s)" in answer
    assert "Senior Data Engineer" in answer
    assert "Remote, US" in answer
    assert "https://boards.greenhouse.io/acme/jobs/111111" in answer


def test_lever_board_reads_its_own_field_names(monkeypatch):
    """Lever calls a posting 'text' and nests location under categories."""
    patch_board(monkeypatch, LEVER_BODY)
    answer = job_portal({"action": "search", "portal": "lever", "company": "acme"})
    assert "1 open role(s)" in answer
    assert "Frontend Engineer" in answer
    assert "London" in answer
    assert "https://jobs.lever.co/acme/aaa" in answer


def test_each_posting_carries_a_stable_job_key(monkeypatch):
    """The key is what a later tracking phase would dedup on, so it must show."""
    patch_board(monkeypatch, GREENHOUSE_BODY)
    answer = job_portal({"action": "search", "portal": "greenhouse", "company": "acme"})
    from core import job_key

    expected = job_key.make_key(
        "acme", "Senior Data Engineer", "https://boards.greenhouse.io/acme/jobs/111111")
    assert expected in answer


def test_the_report_says_it_only_listed_and_read_nothing(monkeypatch):
    """Listing a board is not reading a posting; the answer must not blur them."""
    patch_board(monkeypatch, GREENHOUSE_BODY)
    answer = job_portal({"action": "search", "portal": "greenhouse", "company": "acme"})
    assert "has not read any posting's full text" in answer
    assert "job_posting_guard" in answer


def test_search_hits_the_right_board_url(monkeypatch):
    seen = patch_board(monkeypatch, GREENHOUSE_BODY)
    job_portal({"action": "search", "portal": "greenhouse", "company": "acme"})
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs"]


def test_query_filters_to_matching_roles(monkeypatch):
    patch_board(monkeypatch, GREENHOUSE_BODY)
    answer = job_portal({"action": "search", "portal": "greenhouse", "company": "acme",
                         "query": "engineer"})
    assert "Senior Data Engineer" in answer
    assert "Abuse Investigator" not in answer
    assert "1 matched the filter" in answer


# --------------------------------------------------------------------------- #
# Nothing to report is still reported honestly
# --------------------------------------------------------------------------- #

def test_a_consent_refusal_is_named_as_consent_not_as_failure(monkeypatch):
    patch_board(monkeypatch, {
        "ok": False, "error": "robots policy does not permit",
        "refused_by_policy": True, "hint": "..."})
    answer = job_portal({"action": "search", "portal": "greenhouse", "company": "acme"})
    assert "consent decision, not a network fault" in answer
    assert "retrying will not help" in answer
    assert "open role" not in answer  # nothing was listed


def test_a_transport_failure_is_reported_as_one(monkeypatch):
    patch_board(monkeypatch, {"ok": False, "error": "curl exit 6"})
    answer = job_portal({"action": "search", "portal": "greenhouse", "company": "acme"})
    assert "could not reach" in answer
    assert "curl exit 6" in answer


def test_an_empty_board_is_the_boards_own_answer_not_a_fault(monkeypatch):
    patch_board(monkeypatch, {"ok": True, "code": 200, "media": "json",
                              "consent": "ALLOWED", "data": {"jobs": []}})
    answer = job_portal({"action": "search", "portal": "greenhouse", "company": "acme"})
    assert "listed no open roles" in answer
    assert "not a network fault" in answer


def test_a_filter_that_matches_says_so_without_inventing(monkeypatch):
    patch_board(monkeypatch, GREENHOUSE_BODY)
    answer = job_portal({"action": "search", "portal": "greenhouse", "company": "acme",
                         "query": "pilot"})
    assert "no open roles after the filter" in answer


def test_a_board_that_returned_nothing_structural_claims_nothing(monkeypatch):
    """media other than a job list is not a board; say nothing was returned."""
    patch_board(monkeypatch, {"ok": True, "code": 200, "media": "html",
                              "consent": "ALLOWED", "gate": {"text": "no jobs"}})
    answer = job_portal({"action": "search", "portal": "greenhouse", "company": "acme"})
    assert "did not return a list of postings" in answer
    assert "open role" not in answer


# --------------------------------------------------------------------------- #
# Honest refusal of what is not built or not advertised
# --------------------------------------------------------------------------- #

def test_an_unadvertised_portal_is_refused_by_name(monkeypatch):
    seen = patch_board(monkeypatch, GREENHOUSE_BODY)
    answer = job_portal({"action": "search", "portal": "workday", "company": "acme"})
    assert "does not search 'workday'" in answer
    assert "greenhouse, lever" in answer
    assert seen == []  # a refused portal must not even be fetched


def test_a_search_without_a_company_fetches_nothing(monkeypatch):
    seen = patch_board(monkeypatch, GREENHOUSE_BODY)
    answer = job_portal({"action": "search", "portal": "greenhouse"})
    assert "needs the company" in answer
    assert seen == []


@pytest.mark.parametrize("verb, missing", [
    ("draft", "drafting a CV or cover letter"),
    ("apply", "submitting an application"),
])
def test_a_later_phase_verb_is_refused_by_name(verb, missing):
    answer = job_portal({"action": verb, "company": "acme"})
    assert missing in answer
    assert "yet" in answer
    for real in VERBS:
        assert real in answer


def test_check_is_redirected_to_the_integrity_gate():
    """Reading a posting is the guard's job; the pointer must name it."""
    answer = job_portal({"action": "check", "text": "whatever"})
    assert "job_posting_guard" in answer
    assert "not what job_portal does" in answer


def test_an_unrelated_verb_is_refused_without_pretending():
    answer = job_portal({"action": "summarise"})
    assert "Unknown job_portal action 'summarise'" in answer


def test_portals_advertises_exactly_the_proven_boards():
    answer = job_portal({"action": "portals"})
    for name in PORTALS:
        assert name in answer
    assert "ashby" not in answer.lower()      # never claimed
    assert "workday" not in answer.lower()    # never claimed
    # Honest boundary: listing and local tracking are on offer, drafting and
    # submitting an application are explicitly not.
    assert "lists roles and can track one" in answer
    assert "never drafts material or submits an application" in answer


# --------------------------------------------------------------------------- #
# track: the one verb that writes, and what it writes is a local note only
# --------------------------------------------------------------------------- #

@pytest.fixture
def job_state(tmp_path, monkeypatch):
    """Point job_key's personal state at a throwaway file, per test.

    record_seen/load_seen resolve their path through state_path(), so patching
    that one function keeps every tracking write inside tmp_path and off the
    real user vault.
    """
    from core import job_key

    target = tmp_path / "seen_jobs.json"
    monkeypatch.setattr(job_key, "state_path", lambda: target)
    return target


def test_track_records_a_role_locally(job_state):
    answer = job_portal({"action": "track", "mode": "record",
                         "company": "acme", "title": "Senior Data Engineer",
                         "link": "https://boards.greenhouse.io/acme/jobs/111111"})
    assert "Tracking saved locally" in answer
    assert "Senior Data Engineer" in answer
    assert "did not apply" in answer


def test_track_stores_under_the_same_key_a_search_prints(job_state):
    """A tracked role must dedup on the identical key search reported, not a near-duplicate."""
    from core import job_key

    job_portal({"action": "track", "mode": "record",
                "company": "acme", "title": "Senior Data Engineer",
                "url": "https://boards.greenhouse.io/acme/jobs/111111"})
    expected = job_key.make_key(
        "acme", "Senior Data Engineer", "https://boards.greenhouse.io/acme/jobs/111111")
    seen = job_key.load_seen()
    assert expected in seen
    assert seen[expected]["title"] == "Senior Data Engineer"


def test_track_list_shows_saved_roles(job_state):
    job_portal({"action": "track", "mode": "record",
                "company": "acme", "title": "Backend Engineer"})
    answer = job_portal({"action": "track", "mode": "list"})
    assert "1 role(s) tracked locally" in answer
    assert "Backend Engineer" in answer
    assert "not applications" in answer  # a saved note is never an application


def test_track_list_with_nothing_saved_says_so(job_state):
    answer = job_portal({"action": "track", "mode": "list"})
    assert "Nothing is tracked yet" in answer


def test_track_record_without_company_and_title_saves_nothing(job_state):
    from core import job_key

    answer = job_portal({"action": "track", "mode": "record", "company": "acme"})
    assert "needs a company and a title" in answer
    assert job_key.load_seen() == {}


def test_track_mode_infers_record_when_a_target_is_given(job_state):
    # No explicit mode, but company + title present: it should record.
    answer = job_portal({"action": "track", "company": "acme", "title": "Ops Lead"})
    assert "Tracking saved locally" in answer


def test_track_unknown_mode_is_named_not_guessed(job_state):
    answer = job_portal({"action": "track", "mode": "abolish"})
    assert "does not know mode 'abolish'" in answer


# --------------------------------------------------------------------------- #
# The vocabularies must not drift apart
# --------------------------------------------------------------------------- #

def test_the_three_vocabularies_advertise_the_same_verbs():
    answerable = set(VERBS)
    assert answerable, "the router advertises no verbs at all"
    assert _schema_verbs() == answerable, "main.py advertises verbs the tool cannot answer"
    assert _planner_verbs() == answerable, "agent/planner.py advertises verbs the tool cannot answer"


def test_main_py_routes_the_tool_to_the_action_that_implements_it():
    source = _source("main.py")
    assert 'elif name == "job_portal":' in source
    assert "from actions.job_portal import job_portal as portal_tool" in source


def test_the_agent_executor_routes_the_same_tool():
    source = _source("agent/executor.py")
    assert 'elif tool == "job_portal":' in source
    assert "from actions.job_portal import job_portal" in source


# --------------------------------------------------------------------------- #
# The live probe: standing proof the discovery path is real, not a fixture
# --------------------------------------------------------------------------- #

def test_the_ingress_turns_a_real_greenhouse_board_into_listed_roles():
    """Run by default. Ask one keyless Greenhouse board and prove the whole
    path -- consent gate, fetch, JSON parse, report -- actually works live.

    A board whose postings come and go is fine; that the fetch is *permitted*
    and *structured* is the claim under test. If this ever reads as a consent
    refusal or a transport error, the ingress -- not the board -- has broken,
    which is exactly the regression the subprocess pipe defect introduced once
    already.
    """
    answer = job_portal({"action": "search", "portal": "greenhouse", "company": "gitlab"})
    assert "consent decision" not in answer, answer
    assert "could not reach" not in answer, answer
    assert "open role(s)" in answer, answer
