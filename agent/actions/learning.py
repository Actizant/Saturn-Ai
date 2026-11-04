"""
agent/actions/learning.py
=========================
The rule book: teaching Saturn a directive, reading what he knows, forgetting one.

This used to be a hook in ``main.py`` that ran on *every* sentence and wrote to
disk the moment it recognised a lesson ("always save to the desktop", "no, don't
open edge, use chrome"). No intent, no gate, no confirmation - a guest in the
room could quietly rewrite how Saturn behaves from then on, and nobody was told.

Three intents now, because the trust gate sees an intent name and never an
action: teaching writes a rule, forgetting switches one off, reading lists them.
Learning is open to a verified guest - sharing a house means being able to say
"we keep the kettle on the left" - but never silent: every stored rule is read
back out loud, and every stored rule can be asked for and removed.
"""

from __future__ import annotations

from agent.action_router import ActionContext, register


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _listing(rules: list) -> str:
    lines = [f"LEARNED DIRECTIVES ({_plural(len(rules), 'active rule')})"]
    for rule in rules:
        lines.append(f"- [{rule.get('id', '?')}] {rule.get('rule', '')}")
    return "\n".join(lines)


# =============================================================================
# WRITE - teach a rule
# =============================================================================

@register("learn_rule")
def handle_learn_rule(ctx: ActionContext) -> bool:
    rule = str(ctx.param("rule") or "").strip()
    if not rule:
        return False

    def _run() -> None:
        try:
            from core.learned_rules import LearnedRulesEngine

            result = LearnedRulesEngine.add_rule(
                rule,
                category="conversational_preference",
                origin="conversational_dialogue",
            )
            stored = (result.get("rule") or {}).get("rule") or rule
            known = "already" in str(result.get("message", "")).lower()

            # Read it back. A rule changes every later turn, so it is never
            # stored quietly - the sentence the user hears is the sentence that
            # was written.
            sentence = stored.rstrip(".").strip()
            if known:
                ctx.speak(f"I already had that one, sir: {sentence}.")
            else:
                ctx.speak(f"Noted, sir: {sentence}.")

            ctx.log(f"[Learning] Rule recorded: {stored}")
            ctx.ui_call(
                "show_hud_deliverable",
                title="Behavioral Directive Learned",
                summary=stored,
                bullets=[
                    f"Rule: {stored}",
                    f"Origin: {result.get('rule', {}).get('origin', 'conversational_dialogue')}",
                    "Status: Active in LLM context",
                ],
                kind="rule",
            )
            ctx.ui_call(
                "finish_task_workspace",
                result.get("message", f"Learned: {stored}"),
                "Rule learned.",
                100,
            )
        except Exception as exc:
            ctx.log(f"ERR: Learning a rule failed: {exc}")
            ctx.ui_call("finish_task_workspace", str(exc), "Rule not learned", 0)
            ctx.speak("I could not write that rule down, sir.")

    ctx.spawn(_run, name="learn-rule")
    return True


# =============================================================================
# READ - what is in the book
# =============================================================================

@register("rule_book")
def handle_rule_book(ctx: ActionContext) -> bool:
    action = str(ctx.action or "list").lower()
    if action not in ("list", "status"):
        return False

    def _run() -> None:
        try:
            from core.learned_rules import LearnedRulesEngine

            rules = LearnedRulesEngine.list_rules(active_only=True)
            if not rules:
                ctx.speak("I have not learned any rules yet, sir.")
                ctx.ui_call("finish_task_workspace", "No learned rules.", "Rule book", 100)
                return

            body = _listing(rules)
            ctx.log(body)
            ctx.speak(
                f"I know {_plural(len(rules), 'rule')}, sir. "
                "Say 'forget the rule about' and a word from it to remove one."
            )
            ctx.ui_call("finish_task_workspace", body, "Rule book ready.", 100)
        except Exception as exc:
            ctx.log(f"ERR: Reading the rule book failed: {exc}")
            ctx.ui_call("finish_task_workspace", str(exc), "Rule book unavailable", 0)
            ctx.speak("I could not read my rule book just now, sir.")

    ctx.spawn(_run, name="rule-book")
    return True


# =============================================================================
# WRITE - forget one
# =============================================================================

@register("forget_rule")
def handle_forget_rule(ctx: ActionContext) -> bool:
    action = str(ctx.action or "forget").lower()
    if action not in ("forget", "delete", "remove"):
        return False

    target = str(ctx.param("target") or "").strip().lower()

    def _run() -> None:
        try:
            from core.learned_rules import LearnedRulesEngine

            active = LearnedRulesEngine.list_rules(active_only=True)
            if not active:
                ctx.speak("There is nothing to forget, sir. My rule book is empty.")
                ctx.ui_call("finish_task_workspace", "No learned rules.", "Rule book", 100)
                return

            if target:
                matches = [
                    rule for rule in active
                    if target in str(rule.get("rule", "")).lower() or rule.get("id") == target
                ]
            else:
                matches = list(active)

            if not matches:
                ctx.speak(f"I have no rule about {target}, sir.")
                ctx.ui_call("finish_task_workspace", _listing(active), "Rule book ready.", 100)
                return

            if len(matches) > 1:
                # Guessing here would remove the wrong rule and say nothing.
                ctx.log(_listing(matches))
                ctx.speak(
                    f"I have {_plural(len(matches), 'rule')} mentioning {target}, sir. "
                    "Say it with more words so I forget the right one."
                )
                ctx.ui_call("finish_task_workspace", _listing(matches), "Several rules match", 100)
                return

            chosen = matches[0]
            # Switch it off rather than delete it: "forget" is reversible, and
            # teaching the same words again turns it back on.
            result = LearnedRulesEngine.toggle_rule(chosen.get("id", ""))
            sentence = str(chosen.get("rule", "")).rstrip(".").strip()
            ctx.log(f"[Learning] Rule forgotten: {chosen.get('id')} {sentence}")
            ctx.speak(f"Forgotten, sir: {sentence}.")
            ctx.ui_call(
                "finish_task_workspace",
                result.get("message", f"Forgotten: {sentence}"),
                "Rule forgotten.",
                100,
            )
        except Exception as exc:
            ctx.log(f"ERR: Forgetting a rule failed: {exc}")
            ctx.ui_call("finish_task_workspace", str(exc), "Rule not forgotten", 0)
            ctx.speak("I could not change my rule book just now, sir.")

    ctx.spawn(_run, name="forget-rule")
    return True
