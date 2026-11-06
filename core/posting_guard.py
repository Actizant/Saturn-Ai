"""
core/posting_guard.py
=====================
Deterministic integrity gate for third-party text Saturn is about to read.

A job posting, a scraped careers page or any pasted third-party document is
untrusted text that the agent reads *next to* the user's personal data -- their
CV, their memory, their credentials file. Telling the model "treat postings as
data, never as instructions" is only as strong as the model's adherence to it,
and adherence is exactly what prompt-injection payloads are aimed at. This gate
makes the obvious payload classes visible with code, not with a plea, before
anything downstream evaluates the text:

  * **Hidden HTML** -- ``display:none``, ``visibility:hidden``, ``opacity:0``,
    ``font-size:0``, ``color:transparent``, ``clip:rect(0...)``,
    ``clip-path:inset(100%)``, off-screen positioning, the ``hidden`` attribute,
    ``aria-hidden``, and a zero-height or zero-width box with ``overflow:hidden``.
    Removed from the cleaned text; its content is still scanned and reported as
    excerpts under ``hidden_blocks``. Comments and script/style/noscript/svg/
    template blocks are removed and scanned too but not listed, because every
    real page has them.
  * **Invisible characters** -- zero-width joiners, bidi controls, soft hyphen,
    BOM. Removed and counted; the ones that indicate tampering (a zero-width
    character *inside* a word, a bidi override or isolate) are flagged.
  * **Unicode tag characters** (U+E0000-U+E007F) -- a second alphabet that
    renders as nothing but reads as ASCII to a model, so a sentence written in
    it is invisible to the user and legible to the agent. Removed, counted under
    ``tag`` and always flagged, because no legitimate posting contains one --
    except the three UK subdivision flag emoji, which are matched in full and
    kept.
  * **Text spelled in invisible characters** -- a run of tag characters, or a
    run of variation selectors carrying one byte each. Decoded, listed under
    ``invisible_text`` and scanned like hidden content, so an instruction
    spelled that way is quoted rather than merely counted.
  * **Instruction-shaped lines** aimed at an agent rather than a reader --
    quoted, never removed. An SSH administrator posting legitimately says "SSH".
  * **URLs that exist only in hidden content** -- listed.
  * **Words mixing Latin with Cyrillic or Greek letters** -- listed as
    look-alike spoofing.

Removal is the defense: hidden content never reaches the cleaned text the agent
reads. The warning in the verdict is for the *user* and fires only on signals
that indicate targeting; hidden blocks and hidden URLs are reported as context,
because navigation chrome is hidden on nearly every real page.

**It advises; it never blocks.** A posting that mentions ``.env`` for a DevOps
role is quoted, not rejected. Blocking is a separate decision made by the
capability that received the text, with the user in the loop.

This gate is ported and adapted from the job-search posting integrity gate
described in ``NOTICE`` (MIT); the file next to this one, ``posting_guard.LICENSE``,
carries the upstream copyright and permission notice. The payload classes, the
thresholds and the walk rules are that gate's; the code-point constants, the
Saturn-facing entry points and the reporting prose are Saturn's, and the whole
module was verified case-by-case against the original behaviour it replaces.
Every invisible-character class here is defined by explicit code point rather
than by literal glyphs in the source: the original writes them as raw
characters, which makes the constants invisible to review and puts U+2028 and
U+2029 inside the file -- and ``str.splitlines()`` treats those as line breaks,
so tooling that reads it line by line sees a truncated string.

Usage from Python:
    from core.posting_guard import guard_text, guard_file
    report = guard_file(r"C:\\path\\to\\posting.html")
    print(report["verdict"])

Usage from a shell, for an operator self-test:
    python -m core.posting_guard path/to/posting.html
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# How much of a finding is quoted back. A line is long enough to show the
# trigger; an excerpt longer than this is noise in a terminal.
EXCERPT = 120
LINE_MAX = 200

# --------------------------------------------------------------------------- #
# Invisible-character classes, by code point (see module docstring for why)
# --------------------------------------------------------------------------- #

#: U+200B-U+200D zero-width space/joiner/non-joiner, U+2060 word joiner,
#: U+2061-U+2064 invisible math operators.
ZERO_WIDTH = "".join(chr(c) for c in range(0x200B, 0x200E)) + \
             "".join(chr(c) for c in range(0x2060, 0x2065))

#: U+200E/U+200F LRM/RLM, U+202A-U+202E embedding through RLO,
#: U+2066-U+2069 the three isolates and their terminator.
BIDI = "".join(chr(c) for c in range(0x200E, 0x2010)) + \
       "".join(chr(c) for c in range(0x202A, 0x202F)) + \
       "".join(chr(c) for c in range(0x2066, 0x206A))

#: Bidi overrides and isolates only. These reverse or re-scope the visual order
#: of a line, which no job ad needs; the marks above are everyday RTL text.
BIDI_TAMPER = "".join(chr(c) for c in (0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069))

#: U+00AD soft hyphen, U+2028 line separator, U+2029 paragraph separator.
FORMAT = "".join(chr(c) for c in (0x00AD, 0x2028, 0x2029))

BOM = "\uFEFF"

#: U+E0000-U+E007F: the Unicode "tags" block, one invisible code point per ASCII
#: character. A payload written in it survives copy-paste, never renders, and a
#: model decodes it as if it were the ASCII it mirrors ("ASCII smuggling"). A
#: single one inside the word "ignore" also splits that word past every
#: instruction pattern.
TAG_CHARS = "".join(chr(c) for c in range(0xE0000, 0xE0080))

INVISIBLE = ZERO_WIDTH + BIDI + FORMAT + BOM + TAG_CHARS
TAG_RUN = re.compile("[\U000E0000-\U000E007F]+")

# The one legitimate use of tag characters: the England, Scotland and Wales
# flags (black flag, tag letters gbeng/gbsct/gbwls, cancel tag). Matched in full
# and left intact. A payload inside a well-formed tag sequence, extra tags after
# a valid flag, or a non-RGI subdivision still counts and alarms.
RGI_TAG_FLAG = re.compile(
    "\U0001F3F4\U000E0067\U000E0062(?:\U000E0065\U000E006E\U000E0067"
    "|\U000E0073\U000E0063\U000E0074|\U000E0077\U000E006C\U000E0073)\U000E007F"
)

# Variation selectors (U+FE00-U+FE0F, U+E0100-U+E01EF) pick a glyph variant and
# render as nothing on their own; a run of them carries one byte each. A lone
# one (the VS16 in a heart emoji) is encoding noise; a run is a payload.
VARIATION = re.compile("[\uFE00-\uFE0F\U000E0100-\U000E01EF]")
VARIATION_RUN = re.compile("[\uFE00-\uFE0F\U000E0100-\U000E01EF]{2,}")

# --------------------------------------------------------------------------- #
# HTML: what counts as hidden, and how the document is walked
# --------------------------------------------------------------------------- #

HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|opacity\s*:\s*0(?![.\d]*[1-9])"
    r"|font-size\s*:\s*0(?:\.0+)?(?:px|pt|em|rem|%)?(?![\d.])"
    r"|color\s*:\s*transparent"
    r"|clip\s*:\s*rect\(\s*0"
    r"|clip-path\s*:\s*inset\(\s*100%"
    r"|(?:left|top|text-indent)\s*:\s*-\d{4,}px",
    re.I,
)
# A box collapsed to nothing with its overflow clipped hides its content as
# surely as display:none. Either half alone is everyday layout CSS --
# overflow:hidden is on most cards, height:0 is a spacer -- so both must match.
COLLAPSED_BOX = re.compile(r"\b(?:max-)?(?:height|width)\s*:\s*0(?:\.0+)?(?:px|em|rem|%|pt)?(?![\d.])", re.I)
CLIPPED = re.compile(r"\boverflow(?:-[xy])?\s*:\s*hidden\b", re.I)
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)

PRESTRIP = re.compile(r"<!DOCTYPE[^>]*>|<\?[^>]*\?>", re.I)
COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
DROP_BLOCKS = re.compile(r"<(script|style|noscript|svg|template)\b[^>]*>.*?(?:</\1\s*>|\Z)", re.I | re.S)
TAG = re.compile(r"<(/?)([a-zA-Z][\w:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.S)
ATTR = re.compile(r"([^\s=/\"'>]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>\"']+)))?")

VOID = {"br", "img", "input", "meta", "link", "hr", "wbr", "source", "area", "base",
        "col", "embed", "param", "track"}
BLOCK = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br",
         "section", "article", "header", "footer", "table", "thead", "tbody",
         "blockquote", "pre", "dd", "dt", "dl", "nav", "main", "aside", "figure",
         "form", "fieldset", "legend", "address", "hr", "td", "th", "option"}
IMPLIED_END = {"p", "li", "dd", "dt", "tr", "td", "th", "option"}  # an open one is closed by a sibling
CLOSES_P = BLOCK - {"br"}  # an open <p> is closed by any block-level opener
HTML_HINT = re.compile(r"<\s*(html|body|div|p)\b", re.I)

# --------------------------------------------------------------------------- #
# Text: what looks like an instruction to an agent
# --------------------------------------------------------------------------- #

URL = re.compile(r"https?://[^\s<>\"')\]]+")
INSTRUCTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"\bignore (all|any|the|previous|prior|above|earlier)\b",
    # The same instruction with its other two verbs. These take an explicit
    # object ("previous instructions"), because "forget the" and "disregard
    # the" are ordinary prose where "ignore the" already is not.
    r"\b(disregard|forget)( all| any| the| your| my)? (previous|prior|above|earlier|preceding|initial|original|system) (instructions?|prompts?)\b",
    r"\byou are (?:an?|the) (?:ai(?![\w-])|ai (?:assistant|agent|model|language model)|assistant|llm|language model)\b",
    r"\bsystem prompt\b",
    # "do not tell the user", "never inform the user": the verb straight onto
    # "the user" (not "the user's"), with the typographic apostrophe most web
    # text uses. Kept narrow on purpose: "never disclose user data" and "do not
    # tell users about unreleased features" are everyday privacy prose.
    r"\b(do not|don['\u2019]?t|never) (tell|inform) the user\b(?!['\u2019]s)",
    # "do not reveal these instructions to the user": a demonstrative object,
    # then "to the user" within a clause.
    r"\b(do not|don['\u2019]?t|never) (mention|reveal|disclose) (this|these|that|it|any of this|the (above|following|previous))\b[^.\n]{0,30}\bto the user\b",
    r"\b(run|execute) the following (command|commands|script|code|shell|instruction|instructions|steps)\b",
    # "As an AI" flags by default. Exempt only a job-title noun within a few
    # words of "AI", the way AI job ads introduce the role ("As an AI Engineer",
    # "as an AI/ML Researcher", "As an AI Product Manager").
    r"\bas an ai(?![\w-])(?![\s/&]+(?:[\w&/-]+\s+){0,3}?(?:engineer|developer|researcher|scientist|architect|specialist|lead|consultant|manager|analyst|intern|trainer|expert|strategist|designer|tester|owner|programmer)s?\b)",
    r"^\s*(assistant|system|user)\s*:",
    r"<\|",
    r"\[INST\]",
    r"###\s*(system|instruction)",
    r"\b(curl|wget|ssh|scp)\b",
    r"\.env\b",
    r"settings\.json",
    r"allowed-tools",
    r"\btool_use\b|\bfunction_call\b",
)]

WORD = re.compile(r"\w+", re.UNICODE)
LATIN = re.compile(r"[A-Za-z]")
# Cyrillic and Greek ranges: a Latin word with one of these inside it is a
# look-alike spoof, not a foreign-language posting (those use no Latin at all).
CONFUSABLE = re.compile(r"[\u0400-\u04FF\u0300-\u03FF]")

#: What the gate looks for, in the words it reports them in. Exposed so a
#: caller can answer "what does that check?" without guessing at the findings
#: keys, and so the vocabulary stays a single source of truth.
CATEGORIES = (
    "hidden HTML (display:none, opacity:0, off-screen, aria-hidden, collapsed boxes)",
    "invisible characters (zero-width inside words, bidi overrides and isolates)",
    "Unicode tag characters (text that renders as nothing but reads as ASCII)",
    "text spelled in invisible characters, decoded and quoted",
    "instruction-shaped lines addressed to an agent rather than a reader",
    "URLs that appear only inside hidden content",
    "words mixing Latin with Cyrillic or Greek letters (look-alike spoofing)",
)


# --------------------------------------------------------------------------- #
# Charset decoding for a saved page
# --------------------------------------------------------------------------- #

PRESCAN_BYTES = 1024
META_CHARSET = re.compile(rb"<meta\b[^>]*?charset\s*=\s*[\"']?\s*([A-Za-z0-9._:-]+)", re.I)

_CODECS = {
    "cp949": ("euc-kr", "cp949", "windows-949", "ks_c_5601-1987", "ks_c_5601-1989",
              "ksc5601", "ksc_5601", "korean", "iso-ir-149", "csksc56011987"),
    "cp932": ("shift_jis", "shift-jis", "sjis", "x-sjis", "ms_kanji", "ms932",
              "windows-31j", "csshiftjis"),
    "euc_jp": ("euc-jp", "x-euc-jp", "cseucpkdfmtjapanese"),
    "iso2022_jp": ("iso-2022-jp", "csiso2022jp"),
    "gb18030": ("gb18030", "gbk", "gb2312", "x-gbk", "chinese", "csgb2312", "iso-ir-58"),
    "big5hkscs": ("big5", "big5-hkscs", "x-x-big5", "cn-big5", "csbig5"),
    "cp1252": ("windows-1252", "cp1252", "x-cp1252", "iso-8859-1", "iso8859-1",
               "iso_8859-1", "latin1", "l1", "ascii", "us-ascii", "cp819", "ibm819"),
    "cp1250": ("windows-1250", "cp1250", "x-cp1250"),
    "cp1251": ("windows-1251", "cp1251", "x-cp1251"),
    "cp1253": ("windows-1253", "cp1253"),
    "cp1254": ("windows-1254", "cp1254", "iso-8859-9", "latin5"),
    "cp1257": ("windows-1257", "cp1257"),
    "iso8859_2": ("iso-8859-2", "latin2", "l2"),
    "iso8859_5": ("iso-8859-5", "cyrillic"),
    "iso8859_15": ("iso-8859-15", "latin9", "l9"),
    "koi8_r": ("koi8-r", "koi8", "koi"),
    "koi8_u": ("koi8-u",),
}
LABEL_TO_CODEC = {label: codec for codec, labels in _CODECS.items() for label in labels}


def declared_codec(raw: bytes) -> Optional[str]:
    """The codec a page declares through a UTF-16 BOM or a <meta> tag, or None."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    match = META_CHARSET.search(raw[:PRESCAN_BYTES])
    if match is None:
        return None
    return LABEL_TO_CODEC.get(match.group(1).decode("ascii").lower())


def decode_page(raw: bytes) -> str:
    """Text of a saved page: UTF-8 when valid, else its declared charset, else UTF-8 with replacement.

    Valid UTF-8 always wins, so a page that decoded cleanly before decodes to
    the same string. Only bytes that are not valid UTF-8 fall back to what the
    page declares, and only the labels above are honoured -- so a page cannot
    select a codec such as UTF-7.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    return raw.decode(declared_codec(raw) or "utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _fold(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\xa0\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_text(text: str) -> Tuple[str, dict]:
    """Remove invisible characters.

    Returns ``(text, {"invisible_chars": {category: count},
    "invisible_flagged": n, "invisible_text": [decoded, ...]})`` where the
    flagged count covers a zero-width or BOM character between two letters
    (splitting a word to dodge a scan) and any bidi override or isolate. A BOM
    at offset 0, emoji joiners, RTL marks, soft hyphens, emoji flag tags and a
    lone variation selector are counted but not flagged: they are encoding
    noise, not tampering. Tag-character runs and variation-selector runs are
    decoded into ``invisible_text`` instead, since what they spell is the
    payload.
    """
    counts: Dict[str, int] = {}
    flagged = 0
    for key, chars in (("zero_width", ZERO_WIDTH), ("bidi", BIDI), ("format", FORMAT), ("bom", BOM)):
        n = sum(text.count(c) for c in chars)
        if n:
            counts[key] = n
    flags = RGI_TAG_FLAG.findall(text)
    unflagged = RGI_TAG_FLAG.sub("", text)  # the text minus its legitimate flags
    tags = sum(1 for ch in unflagged if "\U000E0000" <= ch <= "\U000E007F")
    if tags:
        counts["tag"] = tags
    variation = len(VARIATION.findall(text))
    if variation:
        counts["variation"] = variation
    for i, ch in enumerate(text):
        if ch in BIDI_TAMPER:
            flagged += 1
        elif ch in ZERO_WIDTH or ch == BOM:
            before = text[i - 1] if i else ""
            after = text[i + 1] if i + 1 < len(text) else ""
            if before.isalpha() and after.isalpha():
                flagged += 1
    invisible_text: List[str] = []
    for run in TAG_RUN.findall(unflagged):
        decoded = "".join(chr(ord(c) - 0xE0000) for c in run if 0x20 <= ord(c) - 0xE0000 < 0x7F).strip()
        if decoded:
            invisible_text.append(decoded[:LINE_MAX])
    for run in VARIATION_RUN.findall(text):
        data = bytes(ord(c) - 0xFE00 if ord(c) <= 0xFE0F else ord(c) - 0xE0100 + 16 for c in run)
        decoded = "".join(c for c in data.decode("utf-8", errors="replace") if c.isprintable()).strip()
        if decoded:
            invisible_text.append(decoded[:LINE_MAX])
    text = VARIATION.sub("", text)
    if flags:
        # Strip around the flags so the visible emoji survive intact.
        strip = {ord(c): None for c in INVISIBLE}
        pieces, last = [], 0
        for m in RGI_TAG_FLAG.finditer(text):
            pieces.append(text[last:m.start()].translate(strip))
            pieces.append(m.group(0))
            last = m.end()
        pieces.append(text[last:].translate(strip))
        text = "".join(pieces)
    else:
        text = text.translate({ord(c): None for c in INVISIBLE})
    return _fold(text), {"invisible_chars": counts, "invisible_flagged": flagged,
                         "invisible_text": invisible_text}


def _attrs(raw: str) -> dict:
    out = {}
    for m in ATTR.finditer(raw):
        value = (m.group(2) if m.group(2) is not None
                 else m.group(3) if m.group(3) is not None
                 else (m.group(4) or ""))
        out[m.group(1).lower()] = value
    return out


def _style(attrs: dict) -> str:
    """The style attribute as a browser reads it: entities decoded, CSS comments gone.

    A browser decodes ``display&#58;none`` before parsing the CSS and ignores
    ``/**/`` inside a declaration, so both hid content while a naive regex saw
    neither. Normalise before matching.
    """
    return CSS_COMMENT.sub("", html.unescape(attrs.get("style", "")))


def _is_hidden(raw_attrs: str) -> bool:
    attrs = _attrs(raw_attrs)
    if "hidden" in attrs or attrs.get("aria-hidden", "").strip().lower() == "true":
        return True
    style = _style(attrs)
    if HIDDEN_STYLE.search(style):
        return True
    return bool(COLLAPSED_BOX.search(style) and CLIPPED.search(style))


def clean_html(markup: str) -> Tuple[str, dict]:
    """Visible text and the hidden content that was removed.

    Returns ``(text, findings)`` with ``findings["hidden_blocks"]`` (excerpts of
    hidden elements) and ``findings["hidden_text"]`` (everything removed, for
    scanning; not reported). The walker keeps a name stack with HTML5 implied
    end tags, so a stray closer cannot end a hidden element early and an
    unclosed ``<li>`` or ``<p>`` inside one cannot swallow the rest of the page;
    a ``/>`` on a non-void tag is ignored, as browsers do; unterminated
    comments and script/style blocks hide to the end of input, as browsers do.
    """
    hidden_parts: List[str] = []
    hidden_blocks: List[str] = []

    def collect(match):
        inner = re.sub(r"<!--|-->", " ", match.group(0))
        inner = re.sub(r"<[^>]+>", " ", inner)
        hidden_parts.append(html.unescape(inner))
        return " "

    markup = PRESTRIP.sub(" ", markup)
    markup = COMMENT.sub(collect, markup)
    markup = DROP_BLOCKS.sub(collect, markup)

    visible: List[str] = []
    hidden_buf: List[str] = []
    stack: List[str] = []      # open element names, innermost last
    hidden_at: Optional[int] = None  # stack index of the element that started the hidden run
    pos = 0

    def emit(text):
        (visible if hidden_at is None else hidden_buf).append(text)

    def flush_hidden():
        nonlocal hidden_at
        block = _fold(html.unescape("".join(hidden_buf)))
        hidden_buf.clear()
        hidden_at = None
        if block:
            # The excerpt is quoted to the user, so it carries no invisible
            # characters; the raw block goes to hidden_text, where guard()
            # counts and decodes them. Clean before cutting, or a run of
            # invisible characters at the front would leave an empty excerpt.
            hidden_blocks.append(clean_text(block)[0][:EXCERPT])
            hidden_parts.append(block)

    def pop_to(index):
        del stack[index:]
        if hidden_at is not None and hidden_at >= len(stack):
            flush_hidden()

    def last_index(name):
        for i in range(len(stack) - 1, -1, -1):
            if stack[i] == name:
                return i
        return None

    for m in TAG.finditer(markup):
        emit(markup[pos:m.start()])
        pos = m.end()
        closing, name, raw_attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name in BLOCK:
            emit("\n")
        if closing:
            index = last_index(name)
            if index is not None:
                pop_to(index)
            continue  # a closer with no matching opener is ignored
        if name in VOID:
            continue
        if name in IMPLIED_END and stack and stack[-1] == name:
            pop_to(len(stack) - 1)  # sibling closes the open one
        elif name in CLOSES_P and stack and stack[-1] == "p":
            pop_to(len(stack) - 1)
        stack.append(name)
        if hidden_at is None and _is_hidden(raw_attrs):
            hidden_at = len(stack) - 1
    emit(markup[pos:])
    if hidden_at is not None:
        flush_hidden()

    text = html.unescape("".join(visible))
    return _fold(text), {"hidden_blocks": hidden_blocks, "hidden_text": "\n".join(hidden_parts)}


def _excerpt(line: str, at: int) -> str:
    """Up to LINE_MAX characters of ``line``, always including position ``at``.

    The quote is what lets the user dismiss a flag in a second, so it must show
    the match. A fixed ``line[:LINE_MAX]`` showed the first 200 characters of a
    long paragraph and nothing suspicious when the trigger sat later -- fetched
    paragraphs and portal output routinely run past 200.
    """
    if len(line) <= LINE_MAX:
        return line
    lead = LINE_MAX // 3
    start = max(0, at - lead)
    end = min(len(line), start + LINE_MAX)
    start = max(0, end - LINE_MAX)
    head = "\u2026" if start else ""
    tail = "\u2026" if end < len(line) else ""
    return f"{head}{line[start:end]}{tail}"


def scan(visible: str, hidden: str) -> dict:
    """Instruction-shaped lines, hidden URLs, visible URL count, mixed-script words."""
    seen = set()
    instruction_like: List[str] = []
    for line in visible.splitlines() + hidden.splitlines():
        stripped = line.strip()
        if not stripped or stripped in seen:
            continue
        match = next((m for m in (p.search(stripped) for p in INSTRUCTION_PATTERNS) if m), None)
        if match:
            seen.add(stripped)
            instruction_like.append(_excerpt(stripped, match.start()))
    hidden_urls: List[str] = []
    for url in URL.findall(hidden):
        if url not in hidden_urls:
            hidden_urls.append(url)
    homoglyphs: List[str] = []
    for word in WORD.findall(visible + "\n" + hidden):
        if LATIN.search(word) and CONFUSABLE.search(word) and word not in homoglyphs:
            homoglyphs.append(word)
    return {
        "instruction_like": instruction_like,
        "hidden_urls": hidden_urls,
        "visible_url_count": len(URL.findall(visible)),
        "homoglyph_runs": homoglyphs,
    }


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def verdict(findings: dict) -> str:
    """One line for the user. The warning fires only on targeting signals."""
    alarms = []
    n = len(findings.get("instruction_like") or [])
    if n:
        alarms.append(_plural(n, "instruction-like line", "instruction-like lines"))
    n = int(findings.get("invisible_flagged") or 0)
    if n:
        alarms.append(_plural(n, "invisible character inside words", "invisible characters inside words"))
    n = int((findings.get("invisible_chars") or {}).get("tag") or 0)
    if n:
        alarms.append(_plural(n, "tag character (text hidden from the reader)", "tag characters (text hidden from the reader)"))
    n = len(findings.get("invisible_text") or [])
    if n:
        alarms.append(_plural(n, "string spelled in invisible characters", "strings spelled in invisible characters"))
    n = len(findings.get("homoglyph_runs") or [])
    if n:
        alarms.append(_plural(n, "mixed-script word", "mixed-script words"))

    context = []
    n = len(findings.get("hidden_blocks") or [])
    if n:
        context.append(_plural(n, "hidden block", "hidden blocks"))
    n = len(findings.get("hidden_urls") or [])
    if n:
        context.append(_plural(n, "hidden URL", "hidden URLs"))
    removed = " and ".join(context)

    if alarms:
        line = "posting_guard: WARNING " + ", ".join(alarms) + " - review before continuing"
        return f"{line} ({removed} removed)" if removed else line
    if removed:
        return f"posting_guard: clean ({removed} removed, none instruction-like)"
    return "posting_guard: clean"


def guard(raw: str, mode: str = "auto") -> Tuple[str, dict]:
    """Run the whole gate. ``mode`` is "html", "text" or "auto".

    Returns ``(cleaned_text, findings)``. The cleaned text is the only thing a
    caller may hand to a model; the findings are for the user.
    """
    if mode == "auto":
        mode = "html" if HTML_HINT.search(raw) else "text"
    if mode == "html":
        text, hf = clean_html(raw)
        hidden_text, hidden_blocks = hf["hidden_text"], hf["hidden_blocks"]
    else:
        text, hidden_text, hidden_blocks = raw, "", []
    text, tf = clean_text(text)
    hidden_text, hidden_tf = clean_text(hidden_text)
    # Invisible characters inside hidden content count and alarm like visible
    # ones: a tag-spelled instruction in a display:none block is the same
    # payload, only better hidden. What they spell is hidden content -- scanned
    # and reported, never part of the cleaned text.
    for key, n in hidden_tf["invisible_chars"].items():
        tf["invisible_chars"][key] = tf["invisible_chars"].get(key, 0) + n
    tf["invisible_flagged"] += hidden_tf["invisible_flagged"]
    tf["invisible_text"] += hidden_tf["invisible_text"]
    hidden_text = "\n".join([hidden_text, *tf["invisible_text"]])
    findings = {"hidden_blocks": hidden_blocks, **tf, **scan(text, hidden_text)}
    findings["verdict"] = verdict(findings)
    findings["mode"] = mode
    return text, findings


def is_suspicious(findings: dict) -> bool:
    """True when the gate saw something that indicates targeting.

    This is the same signal that puts WARNING in the verdict, exposed as a
    boolean so a capability can decide what to do about it -- show the user,
    refuse to act on the text, or carry on. The gate itself never blocks.
    """
    return not verdict(findings).startswith("posting_guard: clean")


def guard_text(raw: str, mode: str = "auto") -> Dict[str, Any]:
    """Saturn-facing entry point for pasted or portal-CLI text.

    Returns ``{"ok", "verdict", "suspicious", "cleaned", "findings",
    "checked_bytes"}``.
    """
    cleaned, findings = guard(raw, mode)
    return {
        "ok": True,
        "verdict": findings["verdict"],
        "suspicious": is_suspicious(findings),
        "cleaned": cleaned,
        "findings": {k: v for k, v in findings.items() if k != "verdict"},
        "checked_bytes": len(raw.encode("utf-8", errors="replace")),
    }


def guard_file(path) -> Dict[str, Any]:
    """Run the gate on a saved page or a text file.

    A file that cannot be read comes back as ``ok: False`` with the reason,
    never as a clean verdict -- an unreadable posting is not a safe one.
    """
    target = Path(path)
    try:
        raw = decode_page(target.read_bytes())
    except OSError as exc:
        return {"ok": False, "error": f"could not read the posting: {exc}", "path": str(target)}
    except Exception as exc:  # noqa: BLE001 -- unreadable is unreadable
        return {"ok": False, "error": f"could not decode the posting: {exc}", "path": str(target)}
    report = guard_text(raw, "html")
    report["path"] = str(target)
    return report


def _main(argv=None) -> int:
    """Operator self-test: python -m core.posting_guard path/to/posting.html"""
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m core.posting_guard <file> [...]")
        print("checks performed:")
        for item in CATEGORIES:
            print(f"  - {item}")
        return 2
    code = 0
    for arg in argv:
        report = guard_file(arg)
        if not report.get("ok"):
            print(f"{arg}: {report.get('error')}")
            code = 2
            continue
        print(f"{arg}: {report['verdict']}")
        for key in ("hidden_blocks", "instruction_like", "invisible_text", "hidden_urls", "homoglyph_runs"):
            for item in report["findings"][key]:
                print(f'    {key}: "{item}"')
        if report["suspicious"]:
            code = max(code, 1)
    return code


if __name__ == "__main__":
    raise SystemExit(_main())
