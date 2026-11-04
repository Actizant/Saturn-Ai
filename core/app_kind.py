"""
core/app_kind.py
=================
Saturn - what kind of application owns the user's window.

Why this module exists
----------------------
Asked "what am I doing?", Saturn answered "no IDE is open" while the user was
sitting in Qoder IDE. Nothing was broken in the model: it was answering from a
brief that carried a window title and a process name and nothing else, and the
process name of every Electron editor is the same Chromium class
(``Chrome_WidgetWin_1``). A process-name allow-list of ``code.exe`` /
``cursor.exe`` / ``pycharm64.exe`` can therefore never see Qoder, Windsurf,
Trae, Kiro or any other Chromium-shell IDE, so the honest answer from its point
of view was "no IDE".

Classification is done from all three signals together - title, process, window
class - because each one alone has a blind spot:

* the title is the most reliable hint for Electron apps, which all share a
  process and class name;
* the process name is the most reliable hint for native apps, whose window
  title is usually just the document;
* the window class disambiguates a few (a ``ConsoleWindowClass`` is a terminal
  no matter what the title says).

The result is a small vocabulary, so a prompt can state the kind instead of
letting the model guess it.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# ── Vocabulary ───────────────────────────────────────────────────────────────

IDE = "ide"
TERMINAL = "terminal"
BROWSER = "browser"
CHAT = "chat"
MEETING = "meeting"
OFFICE = "office"
MEDIA = "media"
MAIL = "mail"
EXPLORER = "explorer"
DESIGN = "design"
NOTES = "notes"
UNKNOWN = "unknown"

#: The phrase the prompt uses for each kind. Kept in plain language because the
#: model repeats it to the user verbatim.
KIND_PHRASES = {
    IDE: "code editor / IDE",
    TERMINAL: "terminal",
    BROWSER: "browser",
    CHAT: "chat app",
    MEETING: "video meeting",
    OFFICE: "document editor",
    MEDIA: "media player",
    MAIL: "email client",
    EXPLORER: "file manager",
    DESIGN: "design tool",
    NOTES: "notes app",
    UNKNOWN: "application",
}

# ── Known applications ───────────────────────────────────────────────────────
#
# ``(hint, canonical name, kind)``. Order matters: the first hit wins, so the
# more specific entries are written before the generic ones. Hints are matched
# on the lower-cased title with word boundaries, so "zed" cannot fire inside
# "organized".
_APP_HINTS: Tuple[Tuple[str, str, str], ...] = (
    # Meetings first: a meeting window is also a chat window, and calling a
    # meeting "chat" would make the assistant offer to mute a microphone it
    # already muted.
    ("zoom meeting", "Zoom", MEETING),
    ("zoom webinar", "Zoom", MEETING),
    ("google meet", "Google Meet", MEETING),
    ("meet.google.com", "Google Meet", MEETING),
    ("microsoft teams", "Microsoft Teams", MEETING),

    # Editors. The first group is the Chromium shell: same process name, same
    # window class, distinguishable only by title.
    ("qoder ide", "Qoder IDE", IDE),
    ("qoder", "Qoder IDE", IDE),
    ("visual studio code", "Visual Studio Code", IDE),
    ("vs code", "Visual Studio Code", IDE),
    ("code - insiders", "Visual Studio Code Insiders", IDE),
    ("cursor", "Cursor", IDE),
    ("windsurf", "Windsurf", IDE),
    ("trae", "Trae", IDE),
    ("kiro", "Kiro", IDE),
    ("fleet", "JetBrains Fleet", IDE),
    ("zed", "Zed", IDE),
    ("pycharm", "PyCharm", IDE),
    ("intellij", "IntelliJ IDEA", IDE),
    ("webstorm", "WebStorm", IDE),
    ("goland", "GoLand", IDE),
    ("clion", "CLion", IDE),
    ("phpstorm", "PhpStorm", IDE),
    ("rubymine", "RubyMine", IDE),
    ("rustrover", "RustRover", IDE),
    ("rider", "JetBrains Rider", IDE),
    ("datagrip", "DataGrip", IDE),
    ("android studio", "Android Studio", IDE),
    ("xcode", "Xcode", IDE),
    ("visual studio", "Visual Studio", IDE),
    ("eclipse", "Eclipse", IDE),
    ("netbeans", "NetBeans", IDE),
    ("sublime text", "Sublime Text", IDE),
    ("notepad++", "Notepad++", IDE),
    ("arduino ide", "Arduino IDE", IDE),
    ("neovim", "Neovim", IDE),
    ("nvim", "Neovim", IDE),
    ("emacs", "Emacs", IDE),
    ("thonny", "Thonny", IDE),
    ("spyder", "Spyder", IDE),
    ("rstudio", "RStudio", IDE),
    ("jupyter", "Jupyter", IDE),

    # Terminals
    ("windows terminal", "Windows Terminal", TERMINAL),
    ("powershell", "PowerShell", TERMINAL),
    ("command prompt", "Command Prompt", TERMINAL),
    ("git bash", "Git Bash", TERMINAL),
    ("alacritty", "Alacritty", TERMINAL),
    ("wezterm", "WezTerm", TERMINAL),
    ("mintty", "Mintty", TERMINAL),

    # Browsers
    ("google chrome", "Google Chrome", BROWSER),
    ("microsoft edge", "Microsoft Edge", BROWSER),
    ("mozilla firefox", "Firefox", BROWSER),
    ("brave", "Brave", BROWSER),
    ("opera", "Opera", BROWSER),
    ("vivaldi", "Vivaldi", BROWSER),

    # Chat
    ("whatsapp", "WhatsApp", CHAT),
    ("telegram", "Telegram", CHAT),
    ("discord", "Discord", CHAT),
    ("slack", "Slack", CHAT),
    ("signal", "Signal", CHAT),
    ("messenger", "Messenger", CHAT),
    ("instagram", "Instagram", CHAT),

    # Office / documents
    ("microsoft word", "Microsoft Word", OFFICE),
    ("microsoft excel", "Microsoft Excel", OFFICE),
    ("microsoft powerpoint", "Microsoft PowerPoint", OFFICE),
    ("onenote", "OneNote", NOTES),
    ("notion", "Notion", NOTES),
    ("obsidian", "Obsidian", NOTES),
    ("google docs", "Google Docs", OFFICE),
    ("google sheets", "Google Sheets", OFFICE),
    ("google slides", "Google Slides", OFFICE),

    # Mail
    ("outlook", "Microsoft Outlook", MAIL),
    ("gmail", "Gmail", MAIL),
    ("thunderbird", "Thunderbird", MAIL),

    # Media
    ("spotify", "Spotify", MEDIA),
    ("vlc media player", "VLC", MEDIA),
    ("youtube", "YouTube", MEDIA),
    ("musicbee", "MusicBee", MEDIA),

    # Design
    ("figma", "Figma", DESIGN),
    ("photoshop", "Adobe Photoshop", DESIGN),
    ("illustrator", "Adobe Illustrator", DESIGN),
    ("blender", "Blender", DESIGN),
    ("canva", "Canva", DESIGN),

    # File manager
    ("file explorer", "File Explorer", EXPLORER),
)

#: Process executable -> kind. Only used when the title says nothing useful.
_PROCESS_KINDS = {
    "code.exe": IDE,
    "code - insiders.exe": IDE,
    "cursor.exe": IDE,
    "windsurf.exe": IDE,
    "trae.exe": IDE,
    "kiro.exe": IDE,
    "qoder.exe": IDE,
    "zed.exe": IDE,
    "pycharm64.exe": IDE,
    "pycharm.exe": IDE,
    "idea64.exe": IDE,
    "webstorm64.exe": IDE,
    "goland64.exe": IDE,
    "clion64.exe": IDE,
    "phpstorm64.exe": IDE,
    "rubymine64.exe": IDE,
    "rustrover64.exe": IDE,
    "rider64.exe": IDE,
    "datagrip64.exe": IDE,
    "studio64.exe": IDE,
    "devenv.exe": IDE,
    "eclipse.exe": IDE,
    "sublime_text.exe": IDE,
    "notepad++.exe": IDE,
    "nvim-qt.exe": IDE,
    "gvim.exe": IDE,
    "windowsterminal.exe": TERMINAL,
    "wt.exe": TERMINAL,
    "powershell.exe": TERMINAL,
    "pwsh.exe": TERMINAL,
    "cmd.exe": TERMINAL,
    "conhost.exe": TERMINAL,
    "alacritty.exe": TERMINAL,
    "wezterm-gui.exe": TERMINAL,
    "chrome.exe": BROWSER,
    "msedge.exe": BROWSER,
    "firefox.exe": BROWSER,
    "brave.exe": BROWSER,
    "opera.exe": BROWSER,
    "vivaldi.exe": BROWSER,
    "discord.exe": CHAT,
    "telegram.exe": CHAT,
    "whatsapp.exe": CHAT,
    "slack.exe": CHAT,
    "signal.exe": CHAT,
    "zoom.exe": MEETING,
    "teams.exe": MEETING,
    "ms-teams.exe": MEETING,
    "webex.exe": MEETING,
    "winword.exe": OFFICE,
    "excel.exe": OFFICE,
    "powerpnt.exe": OFFICE,
    "onenote.exe": NOTES,
    "notion.exe": NOTES,
    "obsidian.exe": NOTES,
    "outlook.exe": MAIL,
    "thunderbird.exe": MAIL,
    "spotify.exe": MEDIA,
    "vlc.exe": MEDIA,
    "mpc-hc64.exe": MEDIA,
    "figma.exe": DESIGN,
    "photoshop.exe": DESIGN,
    "blender.exe": DESIGN,
    "explorer.exe": EXPLORER,
}

#: Window class -> kind, for windows whose title alone is ambiguous.
_CLASS_KINDS = {
    "consolewindowclass": TERMINAL,
    "cascadia_hosting_window_class": TERMINAL,
    "cabinetwclass": EXPLORER,
}

# Word-boundary match so short hints ("zed", "trae") cannot fire inside a
# longer word. Punctuation in the hint is escaped, and a hint may end in a
# non-word character ("notepad++"), so the right edge only vetoes alphanumerics.
def _hint_pattern(hint: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(hint)}(?![a-z0-9])")


_HINT_PATTERNS = tuple((_hint_pattern(h), label, kind) for h, label, kind in _APP_HINTS)


def _norm(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def classify(title: str = "", process: str = "", class_name: str = "") -> str:
    """Return the kind of app in focus: ``ide``, ``terminal``, ``browser``...

    Never raises and never returns ``None``; an unrecognised window is
    :data:`UNKNOWN` so callers can branch without a ``None`` check.
    """
    lowered = _norm(title)
    if lowered:
        for pattern, _label, kind in _HINT_PATTERNS:
            if pattern.search(lowered):
                return kind

    kind = _PROCESS_KINDS.get(_norm(process))
    if kind:
        return kind

    return _CLASS_KINDS.get(_norm(class_name), UNKNOWN)


def label(title: str = "", process: str = "", class_name: str = "") -> str:
    """Best human name for the app in focus, or "" when nothing is known."""
    lowered = _norm(title)
    if lowered:
        for pattern, app_label, _kind in _HINT_PATTERNS:
            if pattern.search(lowered):
                return app_label
    proc = _norm(process)
    if proc:
        # Strip the extension and title-case it: "someapp.exe" -> "Someapp".
        stem = proc.rsplit(".", 1)[0] if proc.endswith(".exe") else proc
        if stem and stem not in ("unknown",):
            return stem[:1].upper() + stem[1:]
    return ""


def phrase(kind: str) -> str:
    """Prompt-ready phrase for a kind returned by :func:`classify`."""
    return KIND_PHRASES.get(kind or UNKNOWN, KIND_PHRASES[UNKNOWN])


def describe(title: str = "", process: str = "", class_name: str = "") -> str:
    """One line naming the app and its kind: ``Qoder IDE - code editor / IDE``.

    Returns "" when the window carries no usable identity, so the caller can
    omit the line entirely rather than emit "unknown".
    """
    kind = classify(title, process, class_name)
    name = label(title, process, class_name)
    if not name:
        return ""
    if kind == UNKNOWN:
        return name
    return f"{name} - {phrase(kind)}"


def is_ide(title: str = "", process: str = "", class_name: str = "") -> bool:
    """True when the focus is a code editor, whatever shell it is built on."""
    return classify(title, process, class_name) == IDE


__all__ = [
    "IDE",
    "TERMINAL",
    "BROWSER",
    "CHAT",
    "MEETING",
    "OFFICE",
    "MEDIA",
    "MAIL",
    "EXPLORER",
    "DESIGN",
    "NOTES",
    "UNKNOWN",
    "KIND_PHRASES",
    "classify",
    "label",
    "phrase",
    "describe",
    "is_ide",
]
