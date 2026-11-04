"""Document ingestion: the sidecar route, the bridge call, and the tool's wording.

The engine ships an OCR hook but is never handed one, so its own ``/ingest`` and
``/documents`` routes only accept text that a caller has already extracted. Read
a screenshot, a scan or a PDF and the parsing has to happen *inside* the
sidecar, which is the only place the hooks exist. ``POST /ingest/file`` is that
transport, and these tests are written against the real sidecar wherever the
behaviour lives there.

The last group covers the assistant-facing action. Its job is to turn an engine
error code into something a person can act on, so those are tested by code
rather than by prose.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

import memory.universal_bridge as ub
from actions import remember_document

ROOT = Path(__file__).resolve().parent.parent
SIDECAR_JS = ROOT / "memory" / "sidecar.js"
NODE_BINARY = shutil.which("node") or shutil.which("node.exe")

API_KEY = "ingest-key"

# Tesseract installs to Program Files and adds nothing to PATH, which is why the
# sidecar resolves it by directory. The test asks the same question.
TESSERACT = shutil.which("tesseract") or next(
    (
        candidate
        for candidate in (
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tesseract-OCR" / "tesseract.exe",
        )
        if candidate.exists()
    ),
    None,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A real sidecar over a throwaway store."""
    if not NODE_BINARY:
        pytest.skip("node is not installed")
    if not SIDECAR_JS.exists():  # pragma: no cover - packaging guard
        pytest.skip("sidecar.js is missing")

    port = _free_port()
    env = {
        **os.environ,
        "SATURN_MEMORY_PORT": str(port),
        "SATURN_MEMORY_HOST": "127.0.0.1",
        "SATURN_MEMORY_FILE": str(tmp_path / "universal.jsonl"),
        "SATURN_MEMORY_KEY": API_KEY,
        "SATURN_MEMORY_EMBED": "off",
        "SATURN_MEMORY_NAMESPACE": "saturn",
    }
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [NODE_BINARY, str(SIDECAR_JS)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        monkeypatch.setenv("SATURN_MEMORY_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("SATURN_MEMORY_PORT", str(port))
        monkeypatch.setenv("SATURN_MEMORY_HOST", "127.0.0.1")
        monkeypatch.setenv("SATURN_MEMORY_KEY", API_KEY)
        monkeypatch.setenv("SATURN_MEMORY_NAMESPACE", "saturn")
        bridge = ub.UniversalBridge()
        deadline = time.time() + 25
        while time.time() < deadline and not bridge.health(timeout=0.5):
            time.sleep(0.15)
        assert bridge.health(), "sidecar did not start"
        yield bridge
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - hostile
            process.kill()


# ── The route and the bridge, against the real sidecar ───────────────────────

def test_ingest_text_file_is_stored_listed_recalled_and_removed(live, tmp_path):
    source = tmp_path / "handbook.md"
    source.write_text(
        "# Handbook\n\nThe sidecar parses documents itself, so OCR lives in one place.\n" * 20,
        encoding="utf-8",
    )

    result = live.ingest_file(str(source))
    assert result.get("ok"), result
    assert result["title"] == "handbook.md"
    assert result["chunks"] >= 1
    assert result["docId"]

    listed = {row["docId"] for row in live.documents()}
    assert result["docId"] in listed

    hits = live._request(
        "POST", "/recall", {"query": "OCR lives in one place", "namespace": "saturn"}
    )["hits"]
    assert hits, "an ingested document must be recallable"
    assert any("parses documents" in hit.get("text", "") for hit in hits)

    assert live.forget_document(result["docId"]) is True
    assert result["docId"] not in {row["docId"] for row in live.documents()}


def test_ingest_rejects_a_relative_path(live):
    # A relative path would resolve against the sidecar's own working directory,
    # which the caller neither controls nor can see.
    status, body = live._request_raw(
        "POST", "/ingest/file", {"path": "handbook.md", "namespace": "saturn"}
    )
    assert status == 400, body
    assert body["error"] == "PATH_MUST_BE_ABSOLUTE"


def test_ingest_rejects_a_directory(live, tmp_path):
    status, body = live._request_raw(
        "POST", "/ingest/file", {"path": str(tmp_path), "namespace": "saturn"}
    )
    assert status == 400, body
    assert body["error"] == "NOT_A_FILE"


def test_ingest_reports_a_missing_file_as_a_read_failure(live, tmp_path):
    missing = tmp_path / "not-there.pdf"
    result = live.ingest_file(str(missing))
    assert result.get("ok") is False
    assert result["error"] == "READ_FAILED"


def test_ingest_keeps_the_document_out_of_another_namespace(live, tmp_path):
    source = tmp_path / "private.md"
    source.write_text("storage only saturn should see\n" * 10, encoding="utf-8")
    result = live.ingest_file(str(source))
    assert result.get("ok"), result

    assert result["docId"] not in {
        row["docId"] for row in live.documents() if row.get("namespace") != "saturn"
    }
    status, _body = live._request_raw(
        "DELETE", f"/documents/{result['docId']}?namespace=origin"
    )
    assert status == 404, "another namespace must not be able to delete this document"


@pytest.mark.skipif(TESSERACT is None, reason="Tesseract is not installed on this host")
def test_ingest_reads_an_image_with_local_ocr(live, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    ImageFont = pytest.importorskip("PIL.ImageFont")

    image = Image.new("RGB", (900, 200), "white")
    font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 44)
    ImageDraw.Draw(image).text((40, 60), "SATURN LOCAL OCR", fill="black", font=font)
    source = tmp_path / "scan.png"
    image.save(source)

    result = live.ingest_file(str(source))
    assert result.get("ok"), result

    hits = live._request(
        "POST", "/recall", {"query": "SATURN LOCAL OCR", "namespace": "saturn"}
    )["hits"]
    assert hits
    assert any("SATURN" in hit.get("text", "").upper() for hit in hits)


def test_ingest_without_an_ocr_engine_explains_itself(tmp_path, monkeypatch):
    """A host with no Tesseract must say so, not answer "HTTP 400"."""
    bridge = ub.UniversalBridge()
    monkeypatch.setattr(
        bridge,
        "_request_raw",
        lambda *args, **kwargs: (
            400,
            {"error": "OCR_FAILED", "message": "Tesseract not found: tesseract"},
        ),
    )
    source = tmp_path / "scan.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n")

    result = bridge.ingest_file(str(source))
    assert result["ok"] is False
    assert result["error"] == "OCR_FAILED"
    assert "Tesseract not found" in result["message"]


def test_ingest_reports_an_unreachable_store(tmp_path, monkeypatch):
    bridge = ub.UniversalBridge()
    monkeypatch.setattr(
        bridge,
        "_request_raw",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("connection refused")),
    )
    result = bridge.ingest_file(str(tmp_path / "anything.txt"))
    assert result["error"] == "UNREACHABLE"


def test_ingest_rejects_an_empty_path():
    bridge = ub.UniversalBridge()
    assert bridge.ingest_file("")["error"] == "PATH_REQUIRED"


# ── The assistant-facing action ──────────────────────────────────────────────

class _StubBridge:
    def __init__(self, result=None, available=True):
        self.result = result or {}
        self._available = available
        self.calls: list[tuple] = []

    def available(self, autostart=True):
        return self._available

    def ingest_file(self, path, title=None, timeout=None):
        self.calls.append((path, title))
        return self.result


def _install(monkeypatch, bridge):
    monkeypatch.setattr(ub, "get_bridge", lambda: bridge)


def test_action_asks_for_a_file_when_none_is_given(monkeypatch):
    _install(monkeypatch, _StubBridge())
    assert "which file" in remember_document.run({}, player=None).lower()


def test_action_reports_a_file_that_is_not_there(monkeypatch, tmp_path):
    _install(monkeypatch, _StubBridge())
    message = remember_document.run(
        {"file_path": str(tmp_path / "ghost.pdf")}, player=None
    )
    assert "can't find" in message.lower()


def test_action_uses_the_uploaded_file_when_no_path_given(monkeypatch, tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("x", encoding="utf-8")

    class _Player:
        current_file = str(source)

    bridge = _StubBridge({"ok": True, "title": "notes.txt", "chunks": 1})
    _install(monkeypatch, bridge)
    message = remember_document.run({}, player=_Player())
    assert "remembered" in message.lower()
    assert bridge.calls and bridge.calls[0][0] == str(source)


def test_action_counts_chunks_in_plain_words(monkeypatch, tmp_path):
    source = tmp_path / "one.txt"
    source.write_text("x", encoding="utf-8")
    _install(monkeypatch, _StubBridge({"ok": True, "title": "one.txt", "chunks": 1}))
    assert "(1 chunk)" in remember_document.run({"file_path": str(source)}, player=None)

    _install(monkeypatch, _StubBridge({"ok": True, "title": "one.txt", "chunks": 3}))
    assert "(3 chunks)" in remember_document.run({"file_path": str(source)}, player=None)


def test_action_turns_an_ocr_failure_into_a_sentence(monkeypatch, tmp_path):
    source = tmp_path / "scan.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n")
    _install(
        monkeypatch,
        _StubBridge({"ok": False, "error": "OCR_FAILED", "message": "Tesseract not found: tesseract"}),
    )
    message = remember_document.run({"file_path": str(source)}, player=None)
    assert "couldn't be read" in message.lower()
    assert "Tesseract not found" in message


def test_action_reports_a_stopped_memory_store(monkeypatch, tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("x", encoding="utf-8")
    _install(monkeypatch, _StubBridge(available=False))
    message = remember_document.run({"file_path": str(source)}, player=None)
    assert "isn't running" in message.lower()
