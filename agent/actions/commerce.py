"""
agent/actions/commerce.py
=========================
Shopping agent: open the last recommended listing, or run a fresh
Amazon/Flipkart comparison and open the winner.

The search branch used to build its ``_run_shopping`` worker and forget to
start it: Saturn said "Scanning e-commerce deals…", painted the task plan, and
then did nothing at all — while falling through into the legacy chain. The
handler now starts the worker and owns the turn.
"""

from __future__ import annotations

from agent.action_router import ActionContext, register

_OPEN_ACTIONS = ("open_item", "open_link", "buy_now")


@register("shopping")
def handle_shopping(ctx: ActionContext) -> bool:
    if ctx.action in _OPEN_ACTIONS:
        return _open_last_item(ctx)
    return _search_and_compare(ctx)


def _open_last_item(ctx: ActionContext) -> bool:
    try:
        from core.shopping_agent import get_shopping_agent
    except Exception as exc:
        ctx.log(f"ERR: Shopping agent unavailable: {exc}")
        return False

    target_mode = str(ctx.param("mode") or "pc")
    try:
        result = get_shopping_agent().open_last_item(
            platform=ctx.param("platform"), mode=target_mode
        )
    except Exception as exc:
        ctx.log(f"ERR: Shopping agent failed: {exc}")
        ctx.speak("I couldn't open that product link, sir.")
        return True

    if result.get("success"):
        title = str((result.get("item") or {}).get("title") or "product")[:40]
        platform = str(result.get("platform") or "store").capitalize()
        destination = "on your phone" if target_mode == "phone" else "in your browser"
        ctx.speak(f"Opened the {platform} page for {title} {destination}, sir.")
    else:
        ctx.speak(result.get("error") or "No recent shopping item found to open, sir.")
    return True


def _search_and_compare(ctx: ActionContext) -> bool:
    query = str(ctx.param("query") or ctx.text)
    platform = str(ctx.param("platform") or "both").lower()

    ctx.speak(ctx.ack(f"Scanning e-commerce deals for {query}, sir."))
    ctx.ui_call(
        "begin_task_workspace",
        f"Shopping Agent: {query}",
        [
            "Search Amazon India & Flipkart",
            "Cross-compare prices & delivery speeds",
            "Extract customer reviews (Rufus-style)",
            "Launch top item link in browser",
            "Generate interactive HUD deliverable",
        ],
        source=ctx.source or "local",
    )

    def _run() -> None:
        try:
            from core.shopping_agent import get_shopping_agent

            agent = get_shopping_agent()
            ctx.ui_call("update_task_workspace", status="Scanning stores...", percent=35)

            if platform == "amazon":
                amazon = agent.search_amazon_playwright(query, limit=4)
                flipkart = []
            elif platform == "flipkart":
                amazon = []
                flipkart = agent.search_flipkart_playwright(query, limit=4)
            else:
                both = agent.search_both_parallel(query, limit_per_store=3)
                amazon = both.get("amazon", [])
                flipkart = both.get("flipkart", [])

            if not amazon and not flipkart:
                message = (
                    f"I couldn't find live listings for '{query}' on Amazon or "
                    "Flipkart right now, sir."
                )
                ctx.ui_call(
                    "update_task_workspace",
                    status="No Products Found",
                    output=message,
                    percent=0,
                )
                ctx.speak(message)
                return

            ctx.ui_call("update_task_workspace", status="Synthesizing deals...", percent=75)

            synthesis = agent.synthesize_rufus_recommendation(query, amazon, flipkart)
            card_html = agent.generate_hud_shopping_card(query, synthesis, amazon, flipkart)
            best = synthesis.get("best_overall")
            verdict = synthesis.get("verdict", "")
            voice_summary = synthesis.get("voice_summary", "")

            # Automatically launch the top recommended product link.
            if best and best.url:
                agent.execute_cart_or_buy(best.url, platform=best.platform, mode="pc")

            price = f"₹{best.price:,.0f}" if best else ""
            best_platform = best.platform.capitalize() if best else ""
            ctx.ui_call(
                "update_task_workspace",
                status="Comparison Complete & Opened in Browser",
                output=(
                    f"Top Pick: {best.title if best else 'Recommended'}\n"
                    f"Price: {price} ({best_platform})\n\n{verdict}"
                ),
                percent=100,
            )
            ctx.ui_call(
                "show_hud_deliverable",
                title=f"Shopping: {query[:28]}",
                summary=verdict,
                bullets=[
                    f"Top Pick: {best.title[:45]} (₹{best.price:,.0f})" if best else "",
                    f"Platform: {best_platform}",
                    f"Deal: {best.badge if best and best.badge else 'In Stock'}",
                    "Status: Opened in browser",
                ],
                file_path=card_html,
                kind="shopping",
            )

            if voice_summary:
                ctx.speak(
                    f"{voice_summary} I have opened the {best_platform or 'store'} "
                    "listing in your browser, sir."
                )
            elif best:
                ctx.speak(
                    f"The best option is {best.title[:45]} on {best_platform} for "
                    f"₹{best.price:,.0f}. I've opened the link in your browser and "
                    "loaded the comparison card on your HUD, sir."
                )
        except Exception as exc:
            ctx.log(f"ERR: Shopping agent failed: {exc}")
            ctx.ui_call(
                "update_task_workspace", status="Search Failed", output=str(exc), percent=0
            )
            ctx.speak(
                "I encountered an issue searching for that item, sir. "
                "Please check the network connection."
            )

    ctx.spawn(_run, name="shopping-agent")
    return True
