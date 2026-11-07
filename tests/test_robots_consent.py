"""
tests/test_robots_consent.py
============================
core/robots_consent.py decides whether Saturn may fetch a page at all.

The rule it exists to enforce is narrower than it looks: browser headers get
Saturn past a firewall on a site whose published policy allows access, and they
must never get it past a refusal. So every test here asks the same question in
a different disguise -- when the policy is missing, unreadable, malformed or
hostile, does the answer stay on the safe side?

Upstream reached these answers through real incidents, and its docstrings name
them: a Turkish Windows box that decoded robots.txt as cp1254 and returned
None, a soft-200 HTML error page that parsed to zero rules and read as
allowed, a UTF-8 BOM that hid the first ``User-agent`` line, and a wildcard
pattern that sent Python's regex engine into exponential backtracking. Those
counterexamples are reproduced here as regression nets.

The equivalence half diffs Saturn against the original ``tools/robots_check.py``
extracted from the owner's downloaded archive, so a future edit to either side
shows up as a disagreement rather than as a silent redefinition of what
"consent" means. That archive is a local provenance file, not a dependency:
without it those tests skip and everything else still runs.

Note on naming: upstream calls this ``robots_check`` and its fetcher
``fetch_page``; Saturn renamed them ``robots_consent`` and ``page_fetch``
partly for clarity and partly because upstream's fetcher resolves its
siblings with a plain ``import robots_check`` -- sharing the name would let a
test compare Saturn's gate against upstream's fetcher, a mixed pair that agrees
with itself and proves nothing.
"""

import subprocess
from pathlib import Path

import pytest

from core import robots_consent as rc
from tests import _upstream_job_search


@pytest.fixture(scope="module")
def upstream():
    mods, reason = _upstream_job_search.load_all()
    if mods is None:
        pytest.skip(reason)
    return mods["robots_check"]


# --------------------------------------------------------------------------- #
# The corpus: every policy shape upstream learned to distrust
# --------------------------------------------------------------------------- #

ROBOTS_BODIES = [
    "",                                              # empty file: valid allow-all
    "   \n\t\n",                                    # whitespace only: same
    "\ufeff\nUser-agent: *\nDisallow: /",           # BOM must not hide the group
    "User-agent: *\n\nDisallow: /private",          # blank line inside a record
    "user-agent: *\r\ndisallow: /foo%20bar",         # CRLF and a percent rule
    "User-agent: Claude-User\nDisallow: /\nUser-agent: *\nAllow: /",
    "User-agent: *\nAllow: /a\nDisallow: /a/b",     # longest match wins
    "User-agent: *\nDisallow: /*.pdf$",             # end anchor
    "User-agent: *\nDisallow: /*a*a*a*a*a*a*a*a*a*b",  # catastrophic regex bait
    "<html><body>error</body></html>",              # soft-200 pretending to be robots
    "User-agent: *\nDisallow: /\xff\xff",           # bytes that will not decode
    "Sitemap: https://x/sitemap.xml\nUser-agent: *\nDisallow: /admin",
    "User-agent: *\nDisallow:",                     # blank Disallow means allow-all
    "# just a comment",
    "Weird-Field: nope\nUser-agent: *\nDisallow: /secret",
]

PATHS = [
    "/", "/foo bar", "/foo", "/a", "/a/b", "/x.pdf", "/x.pdf?y=1", "/admin",
    "/private/a", "/secret", "/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab",
    "/jobs/123", "/Jobs/123",
]

AGENTS = ["*", "Claude-User", "SomeBot"]


class FakeCurl:
    """Stand in for subprocess.run, answering a fixed status and body.

    "boom" as a status means curl itself failed, which upstream and Saturn both
    must read as *no information*, never as permission.

    The same body answers every call because gate() probes twice, once per
    agent, and a test that calls gate() more than once would otherwise run the
    list dry and start comparing "IndexError" against "IndexError" -- a stub
    artefact, not a ruling. patch_sequence() below is for the cases that really
    do need different answers per call.
    """

    def __init__(self, status, body=""):
        self.status = status
        self.body = body
        self.argv = []

    def __call__(self, argv, **kwargs):
        self.argv.append(argv)
        return fake_curl_result(argv, self.status, self.body)


def fake_curl_result(argv, status, body):
    """Build the CompletedProcess-shaped answer _fetch expects from curl.

    _fetch passes ``-o <file>`` and reads the body back off disk (pipes lose
    data on this box's pythoncore), and only the ``-w`` code on stdout. So the
    fake must honour that same split: body to the -o file, code to stdout.
    """
    class Result:
        stderr = "curl failed"

    result = Result()
    result.returncode = 7 if status == "boom" else 0
    if "-o" in argv:
        Path(argv[argv.index("-o") + 1]).write_bytes(
            body.encode("utf-8", errors="replace"))
        result.stdout = str(status)
    else:  # a caller that still reads the body from stdout
        result.stdout = body + "\n" + str(status)
    return result


def patch_curl(monkeypatch, status, body=""):
    fake = FakeCurl(status, body)
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


class FakeCurlSequence(FakeCurl):
    """Hand out (status, body) pairs in order; for the two-agent cases."""

    def __init__(self, responses):
        super().__init__(responses[0][0], responses[0][1])
        self.responses = list(responses)

    def __call__(self, argv, **kwargs):
        self.argv.append(argv)
        status, body = self.responses.pop(0)
        return fake_curl_result(argv, status, body)


def patch_sequence(monkeypatch, responses):
    fake = FakeCurlSequence(responses)
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


# --------------------------------------------------------------------------- #
# Equivalence with the original implementation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("body", ROBOTS_BODIES)
def test_body_shape_questions_agree_with_upstream(upstream, body):
    assert upstream.is_robots_body(body) == rc.is_robots_body(body)
    assert upstream.has_undecodable_rule(body) == rc.has_undecodable_rule(body)
    assert upstream._groups(body) == rc._groups(body)


@pytest.mark.parametrize("body", ROBOTS_BODIES)
@pytest.mark.parametrize("path", PATHS)
def test_the_ruling_agrees_with_upstream_on_every_path(upstream, body, path):
    for agent in AGENTS:
        assert upstream.allowed(body, agent, path) == rc.allowed(body, agent, path), (
            f"{agent!r} on {path!r} against:\n{body}"
        )


@pytest.mark.parametrize("body", ROBOTS_BODIES)
@pytest.mark.parametrize("path", PATHS)
def test_pattern_matching_agrees_with_upstream(upstream, body, path):
    for rules in rc._groups(body).values():
        for _, pattern in rules:
            assert upstream._match(pattern, path) == rc._match(pattern, path), pattern


# --------------------------------------------------------------------------- #
# Fail closed: the decision matrix
# --------------------------------------------------------------------------- #

def test_unreadable_policy_is_not_permission(monkeypatch):
    patch_curl(monkeypatch, "boom")
    rc_code, message = rc.gate("https://example.com/jobs/1")
    assert rc_code == 1
    assert message.startswith("UNCONFIRMED")


def test_missing_policy_is_permission(monkeypatch):
    patch_curl(monkeypatch, 404)
    code, message = rc.gate("https://example.com/jobs/1")
    assert code == 0
    assert "no robots.txt published" in message


def test_a_refusal_is_final_not_a_transport_error(monkeypatch):
    patch_curl(monkeypatch, 200, "User-agent: *\nDisallow: /jobs")
    code, message = rc.gate("https://example.com/jobs/1")
    assert code == 1
    assert message.startswith("DISALLOWED")


def test_a_decline_for_the_identified_agent_blocks_even_when_star_allows(monkeypatch):
    """The cautious reading: a site that named the agent and said no means no.

    The two probes genuinely get different bodies here -- the identified agent
    is refused while ``*`` is welcomed -- which is the whole point, so this is
    the one case that needs a real sequence.
    """
    patch_sequence(monkeypatch, [
        (200, "User-agent: Claude-User\nDisallow: /\nUser-agent: *\nAllow: /"),
        (200, "User-agent: Claude-User\nDisallow: /\nUser-agent: *\nAllow: /"),
    ])
    code, message = rc.gate("https://example.com/jobs/1")
    assert code == 1
    assert "Claude-User" in message


def test_server_error_leaves_permission_unconfirmed(monkeypatch):
    patch_curl(monkeypatch, 500)
    assert rc.gate("https://example.com/jobs/1")[0] == 1


def test_soft_200_html_error_page_does_not_grant_access(monkeypatch):
    """A 200 whose body is not a robots.txt parses to zero rules; zero rules is
    not consent, and this is exactly how upstream found permission invented."""
    patch_curl(monkeypatch, 200, "<html><body>404 style error</body></html>")
    code, message = rc.gate("https://example.com/jobs/1")
    assert code == 1
    assert "not a robots.txt" in message


def test_undecodable_rule_line_is_unreadable_not_permissive(monkeypatch):
    patch_curl(monkeypatch, 200, "User-agent: *\nDisallow: /a\ufffd")
    code, message = rc.gate("https://example.com/jobs/1")
    assert code == 1
    assert "not valid UTF-8" in message


def test_a_byte_order_mark_cannot_hide_a_disallow(monkeypatch):
    patch_curl(monkeypatch, 200, "\ufeffUser-agent: *\nDisallow: /")
    assert rc.gate("https://example.com/jobs/1")[0] == 1


def test_blank_line_inside_a_record_does_not_drop_the_rule(monkeypatch):
    patch_curl(monkeypatch, 200, "User-agent: *\n\nDisallow: /private")
    assert rc.gate("https://example.com/private/jobs")[0] == 1
    assert rc.gate("https://example.com/public/jobs")[0] == 0


def test_a_hostile_pattern_cannot_hang_the_gate():
    """The walk is O(len(pattern) * len(path)); a regex would not survive this.

    The bait is the classic backtracking blowup -- repeated ``b*`` groups and a
    path that matches every ``b`` but never the closing ``x``. A regex here
    costs minutes; the table walk costs milliseconds, on a file the site
    controls (GHSA-xhqf-8q8x-4jwm).
    """
    import time

    pattern = "/a" + "b*" * 24 + "x"
    path = "/a" + "b" * 40 + "a"
    started = time.monotonic()
    assert rc._match(pattern, path) == -1        # must not match, and must say so fast
    assert time.monotonic() - started < 1.0


# --------------------------------------------------------------------------- #
# Input that never reaches the network at all
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://example.com/x",
    "ftp://example.com/pub",
    "javascript:alert(1)",
    "data:text/html,x",
    "about:blank",
    "",
])
def test_non_http_input_is_refused_without_a_request(url, monkeypatch):
    """Nothing here may reach curl: a fetch tool that follows an arbitrary
    scheme is a file reader wearing a network hat."""
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    code, message = rc.gate(url)
    assert code == 1
    assert calls == [], f"gate tried to make a request for {url!r}"


@pytest.mark.parametrize("url", ["https://", "http://", "https:///nohost"])
def test_a_url_without_a_host_is_refused_without_a_request(url):
    code, message = rc.gate(url)
    assert code == 1
    assert "no host" in message


def test_the_probe_only_ever_asks_for_robots_txt(monkeypatch):
    """gate() rebuilds the target as scheme://host/robots.txt, so a caller
    cannot use it to GET an arbitrary path with an arbitrary agent."""
    fake = patch_curl(monkeypatch, 404)
    rc.gate("https://example.com/admin/credentials?dump=1")
    # A 404 answers on the first probe, so exactly one request is made -- and
    # it is the policy file, never the path the caller handed in.
    assert len(fake.argv) == 1
    for argv in fake.argv:
        assert argv[-1] == "https://example.com/robots.txt"
        assert "--proto" in argv and "=http,https" in argv
        assert "--proto-redir" in argv


def test_query_string_is_part_of_the_path_under_judgement(monkeypatch):
    patch_curl(monkeypatch, 200, "User-agent: *\nDisallow: /search?q=")
    assert rc.gate("https://example.com/search?q=secret")[0] == 1
    assert rc.gate("https://example.com/search?other=1")[0] == 0
