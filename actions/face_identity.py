"""Face identity as four tools: remember, recognise, forget, report.

``remember_face`` - take a few frames from the camera and keep a descriptor
``who_is_this``   - say whose face is in view, or say that it is nobody known
``forget_face``   - delete one person's descriptor
``face_status``   - what works here, who is enrolled, and where it is kept

What is stored is 128 numbers per person, written to the local app-data folder.
Photographs are never saved, and nothing is uploaded. A name is only offered
when the comparison clears a stated threshold; below it the answer is that the
face is not recognised.
"""

from __future__ import annotations

from core import face_id

# Voice answers get one line. The longer hint belongs in the status report,
# not in something that has to be spoken.
_SHORT_MISSING = (
    "I can find faces, but I can't name them yet. That needs two ONNX models - "
    "YuNet and SFace - in the models/face folder."
)


def _error_text(result: dict) -> str:
    error = str(result.get("error") or "")
    if error == "FACE_MODEL_MISSING":
        return _SHORT_MISSING
    if error == "FACE_LANDMARKS_MISSING":
        return _SHORT_MISSING
    if error == "OPENCV_UNAVAILABLE":
        return "OpenCV isn't installed, so there is no camera pipeline to use."
    if error == "NOBODY_ENROLLED":
        return "Nobody is enrolled yet. Ask me to remember a face first."
    if error == "NAME_REQUIRED":
        return "Tell me the name to remember this face by."
    return str(result.get("message") or "That didn't work.")


def _camera_error(exc: Exception) -> str:
    return str(exc) or "The camera didn't cooperate."


def remember_face(parameters: dict, player=None) -> str:
    """Enroll the person in front of the camera under the given name."""
    name = str((parameters or {}).get("name") or "").strip()
    if not name:
        return _error_text({"error": "NAME_REQUIRED"})

    info = face_id.available()
    if not info.get("ok"):
        return _error_text(info)
    if not info.get("identity"):
        return _SHORT_MISSING

    try:
        frames = face_id.capture_frames(face_id.ENROLL_FRAMES, require_face=True)
    except RuntimeError as exc:
        return _camera_error(exc)

    result = face_id.enroll(name, frames)
    if not result.get("ok"):
        return _error_text(result)

    samples = result.get("samples") or 0
    plural = "" if samples == 1 else "s"
    return (
        f"Remembered {result['name']} from {samples} clear frame{plural}. "
        "Only the descriptor was saved - no photo, and it stays on this machine."
    )


def who_is_this(parameters: dict, player=None) -> str:
    """Name the person in front of the camera, if the match clears the threshold."""
    info = face_id.available()
    if not info.get("ok"):
        return _error_text(info)
    if not info.get("identity"):
        return _SHORT_MISSING

    try:
        frames = face_id.capture_frames(2)
    except RuntimeError as exc:
        return _camera_error(exc)

    result = face_id.identify(frames)
    if not result.get("ok"):
        return _error_text(result)
    if result.get("name"):
        score = float(result.get("score") or 0.0)
        margin = float(result.get("margin") or 0.0)
        if margin < 0.05:
            return (
                f"That could be {result['name']}, but it is not a clear match: "
                f"{score:.2f}, barely ahead of the others."
            )
        return f"That looks like {result['name']}. Match {score:.2f}."

    reason = str(result.get("reason") or "")
    faces = int(result.get("faces") or 0)
    if reason == "NO_FACE":
        return "There's no face in view."
    if reason == "FACE_UNREADABLE":
        return "There's a face, but I couldn't read it clearly enough to compare."
    score = float(result.get("score") or 0.0)
    threshold = float(result.get("threshold") or face_id.MATCH_THRESHOLD)
    return (
        f"I don't recognise that face. The closest enrolled person scored "
        f"{score:.2f}, below the {threshold:.2f} needed."
    )


def forget_face(parameters: dict, player=None) -> str:
    name = str((parameters or {}).get("name") or "").strip()
    if not name:
        return "Which name should I forget?"
    if face_id.forget(name):
        return f"Forgotten {name}. Their descriptor is deleted."
    return f"There is nobody called {name} in the face list."


def face_status(parameters: dict, player=None) -> str:
    report = face_id.status()
    info = report.get("available") or {}
    if not info.get("ok"):
        return _error_text(info)

    enrolled = report.get("enrolled") or []
    where = report.get("store") or "the app data folder"
    if info.get("identity"):
        if enrolled:
            listed = ", ".join(enrolled)
            plural = "" if len(enrolled) == 1 else "s"
            return (
                f"Face identity is ready. {len(enrolled)} person{plural} enrolled: "
                f"{listed}. Descriptors only, kept at {where}. Nothing leaves "
                "this machine."
            )
        return (
            f"Face identity is ready, with nobody enrolled yet. Ask me to remember "
            f"a face and it will be stored at {where}."
        )

    detector = info.get("detector") or "nothing"
    return (
        f"I can detect faces with {detector}, but naming them needs two model "
        f"files in {info.get('search', ['models/face'])[0]}: YuNet for detection "
        "and SFace for recognition. They are not there yet, so no name will be "
        "guessed."
    )
