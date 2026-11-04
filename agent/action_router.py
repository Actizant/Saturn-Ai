"""
agent/action_router.py
======================
Registry-based action router: the "act" stage of the cognition pipeline.

Before this module, every dispatched intent was handled by a 800-line
``if intent_res.intent == "...":`` ladder inside ``main.py``. That ladder had
three structural problems:

* A handler could only be found by scrolling, so nobody noticed that two of
  them (``vision`` and the ``shopping`` search) built a worker closure and
  then forgot to start it — the branch silently did nothing.
* A branch that forgot to ``return`` fell through into the legacy rule chain
  and the same turn was answered twice.
* Nothing could be tested without building the whole main window.

The router keeps the exact same control flow semantics, but makes each intent
an isolated, individually testable function:

    if router.dispatch(ctx):
        return

A handler receives an :class:`ActionContext` and returns ``True`` when it took
ownership of the turn (stop processing) or ``False`` to deliberately fall
through to the legacy rules (``open_app`` without an app name, an unclaimed
``mobile_connect`` command, …).

Handlers live in ``agent/actions/`` and register themselves with the
``@register`` decorator. Importing :func:`get_action_router` loads them once.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("action_router")
logger.setLevel(logging.INFO)

Handler = Callable[["ActionContext"], bool]

# intent name -> handler
_HANDLERS: Dict[str, Handler] = {}
_LOADED = False


# =============================================================================
# REGISTRATION
# =============================================================================

def register(*intents: str) -> Callable[[Handler], Handler]:
    """Decorator: bind a handler to one or more dispatcher intent names."""

    def _decorator(fn: Handler) -> Handler:
        for intent in intents:
            key = (intent or "").strip().lower()
            if not key:
                continue
            previous = _HANDLERS.get(key)
            if previous is not None and previous is not fn:
                logger.warning(
                    "Intent '%s' re-registered: %s -> %s",
                    key,
                    getattr(previous, "__qualname__", previous),
                    getattr(fn, "__qualname__", fn),
                )
            _HANDLERS[key] = fn
        return fn

    return _decorator


def registered_intents() -> List[str]:
    """Sorted list of intents that currently have a handler (loads lazily)."""
    get_action_router()
    return sorted(_HANDLERS)


def has_handler(intent: str) -> bool:
    get_action_router()
    return (intent or "").strip().lower() in _HANDLERS


# =============================================================================
# CONTEXT
# =============================================================================

@dataclass
class ActionContext:
    """Everything a handler needs, and nothing about how it is built.

    ``app`` is the Saturn main window. Handlers are allowed to talk to it
    (``app.speak``, ``app.ui``, and the few private helpers such as
    ``_handle_saturn_connect_command``) but must not reach any further into
    its state, so the router stays testable with a stub object.
    """

    text: str
    intent: Any  # core.intent_dispatcher.IntentResult (duck-typed on purpose)
    app: Any
    source: str = "local"
    situation: Any = None

    # -- intent accessors -----------------------------------------------------

    @property
    def name(self) -> str:
        return str(getattr(self.intent, "intent", "") or "").strip().lower()

    @property
    def action(self) -> str:
        return str(getattr(self.intent, "action", "") or "").strip().lower()

    @property
    def params(self) -> Dict[str, Any]:
        params = getattr(self.intent, "parameters", None)
        return params if isinstance(params, dict) else {}

    def param(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def ack(self, fallback: str = "") -> str:
        """The dispatcher's spoken acknowledgement, else ``fallback``."""
        return str(getattr(self.intent, "voice_ack", "") or "") or fallback

    @property
    def voice_ack(self) -> str:
        return str(getattr(self.intent, "voice_ack", "") or "")

    # -- side-effect helpers (all degrade gracefully) -------------------------

    def speak(self, message: Any) -> None:
        if not message:
            return
        try:
            self.app.speak(message)
        except Exception as exc:  # a dead voice engine must not kill a handler
            self.log(f"speak failed: {exc}")

    def log(self, message: Any) -> None:
        """Write to the on-screen log if the window exposes one."""
        ui = getattr(self.app, "ui", None)
        writer = getattr(ui, "write_log", None)
        if not callable(writer):
            return
        try:
            writer(str(message))
        except Exception:
            pass

    def has_ui(self, method: str) -> bool:
        ui = getattr(self.app, "ui", None)
        return callable(getattr(ui, method, None))

    def ui_call(self, method: str, *args: Any, **kwargs: Any) -> bool:
        """Call an optional ``ui`` method. Missing method or error -> no-op."""
        ui = getattr(self.app, "ui", None)
        fn = getattr(ui, method, None)
        if not callable(fn):
            return False
        try:
            fn(*args, **kwargs)
            return True
        except Exception as exc:
            logger.debug("ui.%s failed: %s", method, exc)
            return False

    def spawn(self, target: Callable[[], Any], name: Optional[str] = None) -> threading.Thread:
        """Run a worker in a daemon thread (the old inline ``threading`` idiom)."""
        thread = threading.Thread(target=target, daemon=True, name=name)
        thread.start()
        return thread


# =============================================================================
# ROUTER
# =============================================================================

class ActionRouter:
    """Dispatches an :class:`ActionContext` to the handler for its intent."""

    def handler_for(self, intent: str) -> Optional[Handler]:
        get_action_router()
        return _HANDLERS.get((intent or "").strip().lower())

    def admit(self, ctx: ActionContext) -> bool:
        """Ask the trust gate before the hands move.

        Only a *denial of trust* stops the turn here. A pending confirmation is
        deliberately not enforced at this layer: the router has nowhere to keep
        a half-asked question, so acting on it would refuse work the owner had
        already approved. ``confirm`` becomes enforceable once the task graph
        can hold the pending state.

        A gate that cannot answer is not a reason to stop working.
        """
        try:
            from core.trust_gate import evaluate

            decision = evaluate(ctx.name)
        except Exception as exc:
            logger.debug("trust gate unavailable for '%s': %s", ctx.name, exc)
            return True

        if not decision.denied:
            return True

        logger.info("Trust gate refused '%s': %s", ctx.name, decision.reason)
        ctx.log(f"GATE: {decision.reason}")
        if decision.spoken:
            ctx.speak(decision.spoken)
        return False

    def dispatch(self, ctx: ActionContext) -> bool:
        """Return True when the turn was handled and must not continue."""
        handler = self.handler_for(ctx.name)
        if handler is None:
            return False
        if not self.admit(ctx):
            # The refusal *is* the answer for this turn: falling through would
            # let the legacy rule chain do exactly what the gate just refused.
            return True
        try:
            return bool(handler(ctx))
        except Exception as exc:
            # Same contract as the old branch ladder: a broken handler must
            # not raise into the turn, it falls through to the legacy rules.
            logger.warning("Action handler for '%s' failed: %s", ctx.name, exc)
            ctx.log(f"ERR: {ctx.name} handler failed: {exc}")
            return False

    # Convenience used by main.py: build the context and dispatch in one call.
    def dispatch_intent(
        self,
        text: str,
        intent: Any,
        app: Any,
        source: str = "local",
        situation: Any = None,
    ) -> bool:
        return self.dispatch(
            ActionContext(
                text=text,
                intent=intent,
                app=app,
                source=source or "local",
                situation=situation,
            )
        )


_GLOBAL_ROUTER: Optional[ActionRouter] = None


def _load_handlers() -> None:
    """Import every handler module exactly once (registration side-effects)."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    import agent.actions  # noqa: F401  (registers all handlers)


def get_action_router() -> ActionRouter:
    global _GLOBAL_ROUTER
    if _GLOBAL_ROUTER is None:
        _load_handlers()
        _GLOBAL_ROUTER = ActionRouter()
    return _GLOBAL_ROUTER


def dispatch_actions(
    text: str,
    intent: Any,
    app: Any,
    source: str = "local",
    situation: Any = None,
) -> bool:
    """Module-level entry point used by ``main.py``.

    Never raises: an import failure or router fault returns ``False`` so the
    turn degrades to the legacy rule chain instead of dying.
    """
    try:
        return get_action_router().dispatch_intent(
            text=text, intent=intent, app=app, source=source, situation=situation
        )
    except Exception as exc:
        logger.warning("Action router unavailable: %s", exc)
        return False
