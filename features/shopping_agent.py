"""
Feature: shopping_agent
Description: Autonomous e-commerce shopping agent for Amazon India and Flipkart.
Finds products, compares prices across stores, synthesizes real customer reviews (Rufus-style),
and prepares items for 1-click cart or purchase.
"""

from typing import Any, Dict

FEATURE_METADATA = {
    "name": "shopping_agent",
    "description": (
        "Autonomous shopping agent for Amazon India and Flipkart. Searches products, "
        "compares prices, analyzes customer reviews, finds the best deal, and generates "
        "interactive HUD shopping cards. Only call when the user wants to buy, search, "
        "or compare consumer products on Amazon or Flipkart."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "The product or specification to search for (e.g., '65W GaN charger under 2000', 'wireless mechanical keyboard')."
            },
            "platform": {
                "type": "STRING",
                "description": "Platform to prioritize: 'both' (default), 'amazon', or 'flipkart'."
            },
            "action": {
                "type": "STRING",
                "description": "Action to perform: 'search_and_compare' (default), 'add_to_cart', or 'buy_now'."
            }
        },
        "required": ["query"]
    },
    "version": "1.0.0",
    "active": True
}


def execute(**kwargs: Any) -> Dict[str, Any]:
    query = kwargs.get("query", "").strip()
    if not query:
        return {"error": "No product query specified. Please specify what you would like to search for."}

    platform = kwargs.get("platform", "both").lower()
    action = kwargs.get("action", "search_and_compare").lower()

    from core.shopping_agent import get_shopping_agent
    agent = get_shopping_agent()

    try:
        if platform == "amazon":
            amz_items = agent.search_amazon_playwright(query, limit=5)
            flp_items = []
        elif platform == "flipkart":
            amz_items = []
            flp_items = agent.search_flipkart_playwright(query, limit=5)
        else:
            res = agent.search_both_parallel(query, limit_per_store=3)
            amz_items = res.get("amazon", [])
            flp_items = res.get("flipkart", [])

        if not amz_items and not flp_items:
            return {
                "error": f"Could not find any live listings for '{query}' on Amazon or Flipkart right now."
            }

        synthesis = agent.synthesize_rufus_recommendation(query, amz_items, flp_items)
        card_path = agent.generate_hud_shopping_card(query, synthesis, amz_items, flp_items)

        best_pick = synthesis.get("best_overall")
        best_title = best_pick.title if best_pick else "Top Recommendation"
        best_price = f"₹{best_pick.price:,.0f}" if best_pick else ""
        best_platform = best_pick.platform.capitalize() if best_pick else ""

        return {
            "query": query,
            "verdict": synthesis.get("verdict", ""),
            "voice_summary": synthesis.get("voice_summary", ""),
            "deliverable_html": card_path,
            "best_overall": {
                "title": best_title,
                "price": best_price,
                "platform": best_platform,
                "url": best_pick.url if best_pick else "",
            },
            "total_options": len(amz_items) + len(flp_items),
        }
    except Exception as exc:
        return {"error": f"Shopping agent encountered an error: {str(exc)}"}
