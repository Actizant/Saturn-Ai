"""
core/gemini_models.py
=====================
One place that knows which Gemini models the owner's key can actually call.

Google retires model names, and a hardcoded name turns into ``404 ... is no
longer available to new users``, which reads like a bug in our own code. On
2026-10-30 every name in the 2.5 generation answered 404 for this key while
3.5-3.8 answered normally - so the repair shop's ladder ("2.5-flash, 2.0-flash,
1.5-flash") had no working rung, and because ``llm_client`` and the shim used
the same name, the general fallback was dead too. The log blamed a JSON parser.

Ask for a *tier*, not a name. Callers that can retry use ``ladder()``; callers
that can only make one call use ``resolve()``. Passing an old name is not an
error: ``ladder("gemini-2.5-flash")`` starts with its living replacement.

The ladder is also the throttle list. Free-tier quota is counted per model, so
a rung that is rate-limited still has siblings with budget - which is why
``generate()`` climbs on a 429 and not only on a retired name.
"""

from __future__ import annotations

from typing import Optional, Tuple

# Verified against the live API on 2026-10-30: these answered, in this order of
# preference. Newest first, because a retired name is usually a generation
# behind and the replacement is named by Google's own 404 message.
FLASH: Tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
)

# Cheaper model for classification and bulk work. Falls back to the flash
# ladder rather than to another lite name we have not seen answer.
LITE: Tuple[str, ...] = ("gemini-3.1-flash-lite",) + FLASH

# Retired names still found in the codebase, mapped to what to try instead.
# Keeping the mapping means an old literal is a working call, not a 404.
RETIRED = {
    "gemini-2.5-flash": FLASH[0],
    "gemini-2.5-flash-lite": LITE[0],
    "gemini-2.0-flash": FLASH[0],
    "gemini-2.0-flash-lite": LITE[0],
    "gemini-1.5-flash": FLASH[0],
    "gemini-1.5-pro": FLASH[0],
    "gemini-1.0-pro": FLASH[0],
}

# Aliases Google moves under us. Listed here so callers stop depending on them.
UNSTABLE = {"gemini-flash-latest", "gemini-flash-lite-latest", "gemini-pro-latest"}

# Words a caller may pass instead of a model name: "what tier do you want".
TIER_FLASH = {"flash", "text", "default"}
TIER_LITE = {"lite", "cheap", "classify"}


def is_retired(name: Optional[str]) -> bool:
    """True when ``name`` is one of the names we know Google has withdrawn."""
    return (name or "").strip().lower() in RETIRED


def resolve(name: Optional[str] = None) -> str:
    """The single model name to use for ``name`` (or the default tier head)."""
    return ladder(name)[0]


def ladder(name: Optional[str] = None) -> Tuple[str, ...]:
    """Model names to try, best first, without duplicates.

    ``name`` may be a tier (``"lite"``, ``"flash"``) or a model name. A living
    name the caller asked for keeps first place - a module that deliberately
    uses lite should stay on lite. A retired one is replaced by its successor,
    then the rest of the tier follows as retries.
    """
    wanted = (name or "").strip()
    if not wanted or wanted.lower() in TIER_FLASH:
        return FLASH
    if wanted.lower() in TIER_LITE:
        return LITE

    low = wanted.lower()
    base = LITE if "lite" in low else FLASH
    if low in RETIRED:
        head = RETIRED[low]
    elif low in UNSTABLE:
        head = base[0]
    else:
        head = wanted

    return (head,) + tuple(m for m in base if m != head)


def model_missing(exc: BaseException) -> bool:
    """True when the failure was "that name does not exist"."""
    text = str(exc).lower()
    return (
        "not_found" in text
        or "not found" in text
        or "no longer available" in text
    )


def should_climb(exc: BaseException) -> bool:
    """Whether the next rung of the ladder could plausibly answer this.

    Two failures are per-model and worth moving past: a name Google has
    withdrawn, and a rate limit. Free-tier quota is counted per model (20
    requests a day on the top rung, measured 2026-10-30), so a sibling rung
    still has its own budget. An auth problem or a malformed request fails
    identically on every model, so burning three calls on it helps nobody.
    """
    code = getattr(exc, "code", None)
    text = str(exc).lower()

    if code in (401, 403) or "unauthenticated" in text or "permission_denied" in text:
        return False
    if model_missing(exc):
        return True
    return code == 429 or "resource_exhausted" in text


def generate(client, contents, name: Optional[str] = None, config=None):
    """Call down the ladder with ``client`` and return the first answer.

    One name is one point of failure: the top rung can be retired or throttled
    while its siblings are wide open. Callers that cannot loop the ladder
    themselves use this. An error that climbing cannot fix is raised untouched.
    """
    last: Optional[BaseException] = None

    for model in ladder(name):
        kwargs = {"model": model, "contents": contents}
        if config is not None:
            kwargs["config"] = config
        try:
            return client.models.generate_content(**kwargs)
        except Exception as exc:
            if not should_climb(exc):
                raise
            last = exc

    raise last  # every rung failed the same way
