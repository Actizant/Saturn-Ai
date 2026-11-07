"""
core/robots_consent.py
======================
Decide whether Saturn may fetch a page with browser headers at all.

Many corporate, bank and recruiter sites answer a bot-identifying user agent
with 403 while serving the same page to a browser. A browser-header retry gets
past that filter. It must never get past a *refusal*: a site that publishes
``Disallow`` in its robots.txt has answered the question already, and a 403 on
such a site is not a firewall, it is the answer.

This module tells the two apart, so ``core.fetch_page`` can retry on the first
and stop on the second. Every rule is chosen to fail **closed** -- when the
policy cannot be read, permission is unconfirmed and the fetch does not happen:

  * longest-match wins; on equal specificity ``Disallow`` wins
  * a ``Disallow`` for either ``*`` or the identified token blocks the fetch
  * blank lines inside a record do not end it (Python's own ``robotparser``
    drops the rules there, which fails open)
  * one leading UTF-8 byte-order mark is skipped (left in, it hides the first
    ``User-agent`` line, drops that whole group and turns ``Disallow: /`` into
    allow-all)
  * HTTP 404 means no published policy, which *is* permission
  * HTTP 200 whose body is not a robots.txt (a soft-200 HTML error page) parses
    to zero rules, and zero rules would read as allowed -- so it is refused
  * a ``User-agent``/``Allow``/``Disallow`` line holding U+FFFD is a byte that
    did not decode as UTF-8; such a pattern can never match, so the file is
    unreadable rather than permissive
  * only an http(s) URL with a host has a policy that can be checked; anything
    else is unconfirmed without making a request

Ported and adapted from the job-search web-research consent check described in
``NOTICE`` (MIT); ``robots_consent.LICENSE`` carries the upstream copyright and
permission notice. The RFC 9309 rules, the fail-closed decisions and the
counterexamples in the docstrings are the upstream ones. Saturn's adaptation is
the module boundary (``gate()`` returns a verdict instead of exiting), the
coupling to :func:`core.page_fetch.fetch`, and the removal of the CLI wrapper.

Usage:
    from core.robots_consent import gate
    rc, message = gate("https://example.com/jobs/123")   # rc 0 = may fetch
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

# The identity sent on the robots.txt probe and the policy read for it. Kept as
# the upstream token because it is what the sites actually list: a publisher
# that means to block an AI fetcher writes a rule for that name, and reading
# only "*" would let a site that declined the identified agent be fetched
# anyway. This is a consent token, not a claim about who Saturn is.
IDENTIFIED_AGENT = "Claude-User"

BROWSER = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36')


def _fetch(url: str, ua: str):
    """curl, not urllib: some hosts (jobup.ch) hang urllib indefinitely while
    answering curl in under a second, and ``--max-time`` is a hard ceiling."""
    # "--" terminates option parsing, so a URL beginning with a dash can never
    # be read by curl as a flag. gate() rebuilds the target as
    # scheme://host/robots.txt before calling here, so this is hardening for
    # direct callers rather than a hole in the gate path itself.
    # --proto pins the first hop to http(s) and --proto-redir every redirect
    # -L follows, so neither the caller nor the site can steer curl to file:,
    # gopher:, ftp: or any other protocol it was built with (GHSA-h8vc-753p-wpmh).
    #
    # The body goes to a temp file, not a pipe. This box's pythoncore 3.14
    # loses every threaded pipe read: _readerthread calls .decode() on a str,
    # the thread dies, and communicate() hands back None -- with capture_output
    # the robots body vanished and *every* host read as UNCONFIRMED. A file
    # cannot be mis-decoded by a reader thread, so the policy actually arrives.
    # robots.txt is UTF-8 (RFC 9309), and it is decoded here, unpinned by any
    # locale: on Turkish Windows an locale-chosen read died on bytes like 0x81.
    fd, tmp_name = tempfile.mkstemp(prefix=".robots_consent.")
    os.close(fd)  # curl must be able to open it on Windows too
    tmp = Path(tmp_name)
    try:
        r = subprocess.run(
            ['curl', '-sS', '-L', '--proto', '=http,https', '--proto-redir', '=http,https',
             '--max-redirs', '5', '--max-time', '12', '-A', ua,
             '-H', 'Accept: text/plain,*/*', '-o', str(tmp), '-w', '\n%{http_code}',
             '--', url],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # stdout carries only the -w number. Normalise str/bytes by hand: this
        # box's subprocess sometimes hands back a str even in a bytes-mode pipe.
        if r.returncode != 0:
            raise RuntimeError('curl exit %d' % r.returncode)
        raw = r.stdout if isinstance(r.stdout, str) else (r.stdout or b'').decode(
            'ascii', errors='replace')
        code = int(raw.strip() or 0)
        body = tmp.read_bytes().decode('utf-8', errors='replace')
    finally:
        tmp.unlink(missing_ok=True)
    return body, code


def is_robots_body(text: str) -> bool:
    """Does this actually look like a robots.txt?

    A misconfigured host can answer /robots.txt with 200 and an HTML error page.
    That body parses to zero rules, and zero rules read as "allowed" -- so a
    soft-200 granted permission that was never given. An empty or whitespace-only
    body IS a valid allow-all under RFC 9309 and stays allowed; a non-empty body
    with no recognised directive is treated as unreadable.
    """
    text = text.removeprefix('\ufeff')          # byte-order mark, see _groups
    if not text.strip():
        return True
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip().lower()
        if ':' in line and line.split(':', 1)[0].strip() in (
            'user-agent', 'allow', 'disallow', 'sitemap', 'crawl-delay', 'host',
        ):
            return True
    return False


def has_undecodable_rule(text: str) -> bool:
    """Does a User-agent, Allow or Disallow line hold U+FFFD?

    U+FFFD stands in for a byte that did not decode as UTF-8. A non-conformant
    robots.txt saved in a legacy code page (cp1254, ISO-8859-9) with raw
    non-ASCII bytes in a rule yields a pattern that can never match: the rule
    was silently skipped and the path read as allowed. Such a body is
    unreadable, not permissive. The field is named with U+FFFD removed, so a
    stray byte before "Disallow" cannot hide the line. U+FFFD in a comment or
    in any other field (Sitemap, Crawl-delay, ...) decides nothing and is
    ignored.
    """
    for raw in text.removeprefix('\ufeff').splitlines():
        line = raw.split('#', 1)[0]
        if '\ufffd' in line and ':' in line:
            field = line.split(':', 1)[0].replace('\ufffd', '').strip().lower()
            if field in ('user-agent', 'allow', 'disallow'):
                return True
    return False


def _groups(text: str) -> dict:
    """user-agent -> [(is_allow, pattern)], tolerating blank lines inside a record.

    One leading U+FEFF is skipped, as Google's reference parser does. A file
    saved with a UTF-8 byte-order mark otherwise hid its first field: a leading
    "User-agent: *" went unseen, every rule in its group was dropped for want
    of an agent, and "Disallow: /" read as allow-all.
    """
    out, agents, expect = {}, [], True
    for raw in text.removeprefix('\ufeff').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line or ':' not in line:
            continue
        field, _, value = line.partition(':')
        field, value = field.strip().lower(), value.strip()
        if field == 'user-agent':
            if not expect:
                agents, expect = [], True
            agents.append(value.lower())
            out.setdefault(value.lower(), [])
        elif field in ('allow', 'disallow') and agents:
            expect = False
            for a in agents:
                out[a].append((field == 'allow', value))
    return out


def _match(pattern: str, path: str) -> int:
    """RFC 9309 wildcard match; returns match length or -1.

    The pattern is percent-decoded to match the already-decoded path. Without
    this, "Disallow: /foo%20bar" never matched "/foo bar" and the rule was
    silently skipped -- a fail-open on any site that encodes its own rules.
    Under RFC 9309 section 2.2.2, '$' anchors the end of the URL only when
    at the end of the pattern; mid-pattern '$' is matched literally.

    Matching is a table walk in O(len(pattern) * len(path)), never a regex.
    Each '*' used to become '.*', and a non-matching rule with repeated
    wildcard segments ("/*a*a*a...*b$") sent Python's backtracking engine into
    exponential time -- minutes at 16 segments -- on a file the site controls
    (GHSA-xhqf-8q8x-4jwm). Consecutive '*' match as one, as RFC 9309 implies.
    """
    if pattern == '':
        return -1
    pattern = unquote(pattern)
    end_anchor = pattern.endswith('$')
    pat = re.sub(r'\*{2,}', '*', pattern[:-1] if end_anchor else pattern)
    # reach[j] is True when the first j pattern characters match the path
    # consumed so far. A '*' extends a match over any character (reach[j]
    # carries forward) and also matches nothing (reach[j-1] on the same row).
    reach = [True] + [False] * len(pat)
    for j, c in enumerate(pat, 1):
        if c != '*':
            break
        reach[j] = True
    if reach[-1] and not end_anchor:
        return len(pattern)
    for ch in path:
        step = [False] * (len(pat) + 1)
        for j, c in enumerate(pat, 1):
            if c == '*':
                step[j] = step[j - 1] or reach[j]
            elif c == ch:
                step[j] = reach[j - 1]
        reach = step
        if reach[-1] and not end_anchor:
            return len(pattern)      # a prefix match is enough without '$'
    return len(pattern) if reach[-1] else -1


def allowed(text: str, agent: str, path: str) -> bool:
    g = _groups(text)
    rules = g.get(agent.lower()) or g.get('*') or []
    best_len, best_allow = -1, True
    for is_allow, pat in rules:
        n = _match(pat, path)
        if n > best_len or (n == best_len and n >= 0 and not is_allow):
            best_len, best_allow = n, is_allow      # ties -> Disallow wins (cautious)
    return True if best_len < 0 else best_allow


def gate(url: str) -> tuple[int, str]:
    """(0, reason) when the fetch may proceed, (1, reason) when it may not.

    A non-zero return is not a transport error to retry: it means the site's
    published policy does not permit this path, or that the policy could not be
    read. Either way the caller must go looking for the employer's own posting
    instead of forcing this one.
    """
    parts = urlsplit(url)  # urlsplit lower-cases the scheme
    # Only http(s) with a host is a page this tool can have a policy for; any
    # other input fails closed before curl is asked anything.
    if parts.scheme not in ('http', 'https'):
        return 1, f'UNCONFIRMED (unsupported URL scheme {parts.scheme!r}) - do not fetch'
    if not parts.netloc:
        return 1, 'UNCONFIRMED (no host in the URL) - do not fetch'
    path = unquote(parts.path) or '/'
    if parts.query:
        path += '?' + unquote(parts.query)
    robots = f'{parts.scheme}://{parts.netloc}/robots.txt'
    body, last = None, 'no attempt'
    for ua in (IDENTIFIED_AGENT, BROWSER):
        try:
            text, code = _fetch(robots, ua)
        except Exception as e:  # noqa: BLE001 -- an unreadable policy is not permission
            last = type(e).__name__; continue
        if code == 404:
            return 0, 'ALLOWED - no robots.txt published'
        if code == 200:
            if not is_robots_body(text):
                last = 'HTTP 200 but the body is not a robots.txt'
                continue
            if has_undecodable_rule(text):
                last = 'HTTP 200 but a User-agent/Allow/Disallow line is not valid UTF-8'
                continue
            body = text
            break
        last = 'HTTP %d' % code
    if body is None:
        return 1, f'UNCONFIRMED ({last}) - do not fetch'
    for a in (IDENTIFIED_AGENT, '*'):
        if not allowed(body, a, path):
            return 1, f'DISALLOWED for {a} - do not fetch'
    return 0, 'ALLOWED - robots.txt permits this path'
