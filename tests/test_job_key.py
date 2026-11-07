"""
tests/test_job_key.py
=====================
core/job_key.py turns "company + title" into one stable, filesystem-safe id.

Tracking only means something if one posting is one entry, and the ways that
break are all documented in the upstream code as things that actually happened
in a live workspace: a ``/`` in a job title splitting an archive folder across
directories, and the same Deloitte role stored twice because two runs truncated
its title at different points.

The equivalence half diffs Saturn against the original ``tools/job_key.py``
from the owner's downloaded archive. Saturn's own half pins what it changed:
the state now lives in the user's data directory instead of the repository,
because a list of jobs someone applied to is personal data and must not be
shipped inside an installed application.
"""

import json

import pytest

from core import job_key
from tests import _upstream_job_search


@pytest.fixture(scope="module")
def upstream():
    mods, reason = _upstream_job_search.load_all()
    if mods is None:
        pytest.skip(reason)
    return mods["job_key"]


# The counterexamples upstream found in a real seen_jobs.json, plus the shapes
# that follow from them: path-breaking punctuation, truncation collisions, and
# scripts the ASCII fold cannot represent.
KEY_CASES = [
    pytest.param("deloitte", "Junior Cybersecurity Analyst (OT/IoT)", "", id="slash-in-title"),
    pytest.param("neverhack estonia", "Penetration Tester / Red-Teamer", "", id="spaced-slash"),
    pytest.param("ops consulting, llc", "Malware Analyst", "", id="comma-in-company"),
    pytest.param("deloitte", "Cyber Intelligence Center Security Analy", "", id="truncated-at-40"),
    pytest.param("deloitte", "Cyber Intelligence Center Security Analyst at", "", id="truncated-at-43"),
    pytest.param("securion", "", "https://jobs.example/1234567", id="empty-title"),
    pytest.param("Программист", "1С разработчик", "https://example.ru/jobs/9876543", id="cyrillic"),
    pytest.param("Аналитик", "1С аналитик", "https://example.ru/jobs/1122334", id="cyrillic-rival"),
    pytest.param("Сбер AI", "инженер", "", id="mixed-script-company"),
    pytest.param("Яндекс AI", "инженер", "", id="mixed-script-rival"),
    pytest.param("Ørsted", "Senior Engineer", "", id="latin-without-decomposition"),
    pytest.param("", "", "", id="all-empty"),
    pytest.param("Acme", "x" * 200, "", id="absurd-title"),
    pytest.param("Nordic Firm", "Inzhener OOO Chen",
                 "https://freehire.example/inzhener-ooo-chen-hlk3qjfg", id="no-six-digit-id"),
    pytest.param("ΔΕΙΚΤΗΣ ΑΕ", "Μηχανικός Λογισμικού", "", id="greek"),
    pytest.param("Kiosk", "Café Manager – Paris", "", id="accents-and-dash"),
    pytest.param("Tab\tCompany\nName", "Title: With: Colons", "", id="control-characters"),
    pytest.param("a" * 100, "b" * 200, "", id="over-cap-both-halves"),
]


# --------------------------------------------------------------------------- #
# Equivalence with the original rule
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("company, title, url", KEY_CASES)
def test_the_key_is_exactly_what_upstream_produces(upstream, company, title, url):
    assert job_key.make_key(company, title, url) == upstream.make_key(company, title, url)


@pytest.mark.parametrize("company, title, url", KEY_CASES)
def test_the_predicates_agree_with_upstream(upstream, company, title, url):
    mine = job_key.make_key(company, title, url)
    theirs = upstream.make_key(company, title, url)
    assert job_key.is_canonical(mine) == upstream.is_canonical(theirs)
    assert job_key.is_legacy_shape(mine) == upstream.is_legacy_shape(theirs)


@pytest.mark.parametrize("text", [c[1] or c[0] for c in KEY_CASES])
def test_the_slug_and_the_loss_test_agree(upstream, text):
    assert job_key.slugify(text) == upstream.slugify(text)
    assert job_key._lost_letters(text) == upstream._lost_letters(text)


# --------------------------------------------------------------------------- #
# The properties the rule exists to guarantee
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("company, title, url", KEY_CASES)
def test_a_key_is_stable_across_runs(company, title, url):
    """Two runs must agree, or the file gains a second entry for one job."""
    first = job_key.make_key(company, title, url)
    assert job_key.make_key(company, title, url) == first


@pytest.mark.parametrize("company, title, url", KEY_CASES)
def test_a_key_never_carries_a_path_breaking_character(company, title, url):
    key = job_key.make_key(company, title, url)
    for forbidden in ("/", "\\", ",", ":", "*", "?", '"', "<", ">", "|"):
        assert forbidden not in key, key
    assert " " not in key and key == key.lower()


def test_two_long_titles_that_share_a_prefix_do_not_collide():
    """The exact Deloitte failure: one title truncated, one not, one posting."""
    a = job_key.make_key("deloitte", "Cyber Intelligence Center Security Analy")
    b = job_key.make_key("deloitte", "Cyber Intelligence Center Security Analyst at")
    assert a != b
    assert job_key.is_canonical(a) and job_key.is_canonical(b)


def test_two_cyrillic_titles_that_fold_to_nothing_stay_distinct():
    a = job_key.make_key("Программист", "1С разработчик", "https://x.ru/jobs/9876543")
    b = job_key.make_key("Аналитик", "1С аналитик", "https://x.ru/jobs/1122334")
    assert a != b
    assert not a.endswith("_") and not b.endswith("_"), "a bare company_ prefix keys every RU job alike"


def test_an_absent_title_still_produces_a_usable_key():
    key = job_key.make_key("securion", "", "https://jobs.securion.example/1234567")
    assert job_key.is_canonical(key)
    assert "1234567" in key


# --------------------------------------------------------------------------- #
# Saturn's change of address: the state is personal, not part of the app
# --------------------------------------------------------------------------- #

def test_state_lives_in_the_user_data_directory_outside_the_repo():
    from pathlib import Path

    from core import user_paths

    path = job_key.state_path()
    assert path.is_absolute()
    assert user_paths.get_user_data_dir() in path.parents
    # The installed tree must never hold somebody's application history, which
    # is exactly where upstream kept it.
    repo_root = Path(__file__).resolve().parent.parent
    assert Path(repo_root) not in path.parents
    assert not str(path).lower().startswith(str(repo_root).lower())


def test_record_then_load_round_trips_through_the_canonical_key(tmp_path):
    state = tmp_path / "seen_jobs.json"
    posting = {"company": "Acme Corp", "title": "SOC Analyst (L2)",
               "url": "https://acme.example/jobs/42", "status": "seen"}
    key = job_key.record_seen(posting, path=state)

    assert job_key.is_canonical(key)
    stored = job_key.load_seen(state)
    assert list(stored) == [key]
    assert stored[key]["url"] == "https://acme.example/jobs/42"


def test_recording_the_same_posting_twice_creates_one_entry(tmp_path):
    state = tmp_path / "seen_jobs.json"
    first = job_key.record_seen({"company": "Acme", "title": "Engineer"}, path=state)
    second = job_key.record_seen({"company": "acme ", "title": "  Engineer"}, path=state)
    assert first == second
    assert len(job_key.load_seen(state)) == 1


def test_no_file_yet_is_empty_rather_than_an_error(tmp_path):
    assert job_key.load_seen(tmp_path / "absent.json") == {}


def test_a_corrupt_file_raises_instead_of_looking_like_a_fresh_start(tmp_path):
    """Returning {} here would silently forget every posting already seen, and
    the next run would re-apply to all of them believing they were new."""
    bad = tmp_path / "seen_jobs.json"
    bad.write_text("{not json at all", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        job_key.load_seen(bad)


def test_a_record_of_the_wrong_shape_raises(tmp_path):
    not_a_map = tmp_path / "seen_jobs.json"
    not_a_map.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError):
        job_key.load_seen(not_a_map)


# --------------------------------------------------------------------------- #
# The audit: damage and drift are reported apart
# --------------------------------------------------------------------------- #

def _write_state(tmp_path, entries):
    state = tmp_path / "seen_jobs.json"
    state.write_text(json.dumps({"seen": entries}), encoding="utf-8")
    return state


def test_audit_reports_a_clean_file_as_clean(tmp_path):
    key = job_key.make_key("Acme", "Engineer")
    state = _write_state(tmp_path, {key: {"company": "Acme", "title": "Engineer",
                                          "url": "https://a.example/1"}})
    report = job_key.audit(state)
    assert report["ok"] and report["clean"]
    assert report["entries"] == 1
    assert report["malformed_keys"] == []
    assert report["duplicate_urls"] == {}


def test_audit_catches_path_breaking_keys(tmp_path):
    state = _write_state(tmp_path, {
        "deloitte_junior-cybersecurity-analyst-(ot/iot)":
            {"company": "deloitte", "title": "Junior Cybersecurity Analyst (OT/IoT)"},
    })
    report = job_key.audit(state)
    assert not report["clean"]
    assert report["malformed_keys"] == ["deloitte_junior-cybersecurity-analyst-(ot/iot)"]


def test_audit_catches_one_posting_stored_under_two_keys(tmp_path):
    a = job_key.make_key("deloitte", "Cyber Intelligence Center Security Analy")
    b = job_key.make_key("deloitte", "Cyber Intelligence Center Security Analyst at")
    state = _write_state(tmp_path, {
        a: {"company": "deloitte", "title": "Cyber Intelligence Center Security Analy",
            "url": "https://d.example/job/9"},
        b: {"company": "deloitte", "title": "Cyber Intelligence Center Security Analyst at",
            "url": "https://d.example/job/9"},
    })
    report = job_key.audit(state)
    assert not report["clean"]
    assert list(report["duplicate_urls"]) == ["https://d.example/job/9"]
    assert sorted(report["duplicate_urls"]["https://d.example/job/9"]) == sorted([a, b])


def test_legacy_three_part_keys_are_drift_not_damage(tmp_path):
    """Harmless on disk but not what the rule produces, so the fix stays a
    decision by the user rather than an automatic rewrite of their history."""
    state = _write_state(tmp_path, {"acme_engineer_aarhus": {"company": "Acme",
                                                             "title": "Engineer"}})
    report = job_key.audit(state)
    assert report["clean"], "a legacy key is not damage"
    assert report["legacy_three_part_keys"] == ["acme_engineer_aarhus"]


def test_audit_of_an_unreadable_file_is_an_error_not_a_verdict(tmp_path):
    report = job_key.audit(tmp_path / "does-not-exist.json")
    assert not report["ok"]
    assert "cannot read" in report["error"]
