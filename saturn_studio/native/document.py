"""Saturn Studio -- the ``.saturn`` document format.

A Saturn design file is a zip container named ``<doc>.saturn`` holding three
kinds of entry:

* ``manifest.json`` -- what the file is: the format tag, a version integer, and
  the list of media blobs it carries. This is read first and is what lets a
  future reader refuse a file it cannot understand instead of half-loading it.
* ``document.json`` -- the scene, serialized from :class:`scene.Document`.
* ``media/<name>`` -- any embedded raster the document references. Milestone 1
  does not draw images yet, so this is usually empty, but the container reserves
  it so adding images never changes the on-disk contract.

Writing is atomic: the bytes are assembled in memory, dropped on a temp file in
the same directory, then renamed over the target. A crash mid-save leaves the
old file intact, never a half-written one. Loading is defensive: anything that
is not a ``.saturn`` archive of a known version raises :class:`DocumentError`
rather than producing a document that looks fine and is quietly wrong.

Like the scene it serializes, this module imports no painter.
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from saturn_studio.native import scene

FORMAT = "saturn-studio"
FORMAT_VERSION = 1
MAX_DOC_BYTES = 8 * 1024 * 1024  # a readable ceiling; anything larger is refused, not parsed
MANIFEST = "manifest.json"
DOCUMENT = "document.json"
MEDIA_PREFIX = "media/"


class DocumentError(Exception):
    """A file is not a readable ``.saturn`` document, or is beyond a limit."""


# --------------------------------------------------------------------------- #
# Bytes <-> Document, so the window and the voice layer can move a whole
# document through memory (or the undo stack) as one opaque blob.
# --------------------------------------------------------------------------- #

def document_to_bytes(doc: scene.Document, media: Optional[dict[str, bytes]] = None) -> bytes:
    """Serialize a document (plus any ``{name: bytes}`` media) to container bytes."""
    names = sorted(media or {})
    manifest = {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "saved": datetime.now(timezone.utc).isoformat(),
        "media": names,
    }
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
        archive.writestr(DOCUMENT, json.dumps(doc.to_dict(), indent=2))
        for name in names:
            archive.writestr(MEDIA_PREFIX + name, media[name])  # type: ignore[index]
    return buffer.getvalue()


def document_from_bytes(data: bytes) -> tuple[scene.Document, dict[str, bytes]]:
    """Read container bytes back into a document and its media map.

    Every refusal here is deliberate: a file we only partly understand is worse
    than no file, because the user would then trust a canvas that is quietly
    missing layers.
    """
    if len(data) > MAX_DOC_BYTES:
        raise DocumentError(
            f"document is {len(data) / 1e6:.1f} MB, over the "
            f"{MAX_DOC_BYTES / 1e6:.0f} MB Saturn reads directly"
        )
    from io import BytesIO

    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocumentError("this is not a Saturn document (it is not a valid archive)") from exc

    with archive:
        names = set(archive.namelist())
        if MANIFEST not in names or DOCUMENT not in names:
            raise DocumentError("this is not a Saturn document (no manifest or scene)")
        try:
            manifest = json.loads(archive.read(MANIFEST))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DocumentError("the document's manifest is corrupt") from exc
        if manifest.get("format") != FORMAT:
            raise DocumentError("this file is not a Saturn Studio document")
        version = int(manifest.get("version", 0))
        if version > FORMAT_VERSION:
            raise DocumentError(
                f"this document was written by a newer Saturn (format {version}); "
                f"this build reads up to {FORMAT_VERSION}"
            )
        try:
            raw = json.loads(archive.read(DOCUMENT))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DocumentError("the document's scene data is corrupt") from exc

        media: dict[str, bytes] = {}
        for entry in names:
            if entry.startswith(MEDIA_PREFIX) and entry != MEDIA_PREFIX:
                media[entry[len(MEDIA_PREFIX):]] = archive.read(entry)

    try:
        return scene.Document.from_dict(raw), media
    except Exception as exc:  # noqa: BLE001 -- a bad scene is a bad document
        raise DocumentError(f"the document's scene could not be read: {exc}") from exc


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #

def save_document(doc: scene.Document, path: Path | str,
                  media: Optional[dict[str, bytes]] = None) -> Path:
    """Write ``doc`` to ``<path>.saturn`` atomically and return the real path."""
    target = Path(path).expanduser()
    if target.suffix.lower() != ".saturn":
        target = target.with_suffix(".saturn")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = document_to_bytes(doc, media)
    _atomic_write(target, data)
    return target


def load_document(path: Path | str) -> tuple[scene.Document, dict[str, bytes]]:
    source = Path(path).expanduser()
    if not source.is_file():
        raise DocumentError(f"there is no {source} to open")
    return document_from_bytes(source.read_bytes())


def _atomic_write(target: Path, data: bytes) -> None:
    handle, temp = tempfile.mkstemp(dir=str(target.parent), prefix=target.stem, suffix=".saturn.tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def looks_like_saturn(path: Path | str) -> bool:
    """Cheap check the host uses to tell a Saturn file from a stray ``.fig``."""
    return Path(path).suffix.lower() == ".saturn"
