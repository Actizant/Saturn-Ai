"""Store a document in Saturn's long-term memory, entirely on this machine.

The parsing happens inside the memory sidecar, which is where the OCR hooks
live, so this module is an adapter: resolve the path, ask the bridge to ingest,
and turn the engine's answer into one sentence the assistant can say out loud.

No model, no API key and no VRAM are involved - a text file is read directly, a
PDF is rendered and its text extracted, and an image or a scan goes through the
locally installed Tesseract.
"""

from __future__ import annotations

import os


# The engine's codes are precise but unreadable, and every one of them is a
# normal outcome of "remember this" - a locked file, a photo of handwriting, a
# password-protected PDF. Each gets a sentence a person can act on.
_MESSAGES = {
    "PATH_REQUIRED": "I didn't get a file to remember",
    "PATH_MUST_BE_ABSOLUTE": "that file path wasn't usable",
    "READ_FAILED": "I couldn't read that file",
    "NOT_A_FILE": "that path isn't a file",
    "FILE_TOO_LARGE": "that file is too large to load into memory",
    "OCR_FAILED": "the text in that image couldn't be read",
    "OCR_EMPTY": "I couldn't find any text in it",
    "OCR_REQUIRED": "images and scans need the OCR engine, which isn't set up",
    "PDF_RENDERER_REQUIRED": "scanned PDFs need Poppler, which isn't installed",
    "PDF_PASSWORD_REQUIRED": "that PDF is password-protected",
    "PDF_DECRYPTION_REQUIRED": "that PDF is encrypted",
    "UNSUPPORTED_DOCUMENT_TYPE": "I can't read that format yet",
    "UNREACHABLE": "my memory store isn't running",
}

# These carry a message from the engine (which language pack is missing, how
# many pages failed) that is more useful than the sentence above.
_DETAILED = {"OCR_FAILED", "PDF_PASSWORD_REQUIRED", "READ_FAILED", "FILE_TOO_LARGE"}


def _resolve_path(parameters: dict, player) -> str:
    path = str(parameters.get("file_path") or parameters.get("path") or "").strip()
    if path:
        return path
    # The window remembers whatever was dropped on it, and every other file
    # action falls back to it, so "remember this" works with no path at all.
    try:
        return str(getattr(player, "current_file", "") or "").strip()
    except Exception:
        return ""


def run(parameters: dict, player=None, speak=None, session_memory=None) -> str:
    params = parameters or {}
    path = _resolve_path(params, player)
    if not path:
        return "Tell me which file to remember."

    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        return f"I can't find {os.path.basename(path)}."

    from memory.universal_bridge import get_bridge

    bridge = get_bridge()
    if bridge is None or not bridge.available(autostart=True):
        return "My memory store isn't running, so I can't keep that yet."

    result = bridge.ingest_file(path, title=params.get("title") or None)
    if result.get("ok"):
        title = result.get("title") or os.path.basename(path)
        chunks = int(result.get("chunks") or 0)
        plural = "" if chunks == 1 else "s"
        return f"Remembered {title} ({chunks} chunk{plural}). Ask me about it any time."

    code = str(result.get("error") or "")
    reason = _MESSAGES.get(code, "something went wrong storing it")
    detail = str(result.get("message") or "").strip()
    if code in _DETAILED and detail:
        return f"{reason}: {detail}"
    return f"{reason}."
