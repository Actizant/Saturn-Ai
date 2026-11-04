from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from core.intent_dispatcher import IntentDispatcher
from core.shopping_agent import ShoppingAgent, ShoppingItem


def test_shopping_item_model():
    item = ShoppingItem(
        title="Portronics 65W GaN Charger",
        price=1499.0,
        original_price=2999.0,
        discount_pct=50,
        rating=4.4,
        reviews_count=1250,
        platform="amazon",
        url="https://www.amazon.in/dp/B0EXAMPLE",
        image_url="https://m.media-amazon.com/images/I/test.jpg",
        item_id="B0EXAMPLE",
        badge="Best Seller",
        pros=["Fast charging", "Compact"],
        cons=["Gets warm"]
    )
    d = item.to_dict()
    assert d["title"] == "Portronics 65W GaN Charger"
    assert d["price"] == 1499.0
    assert d["discount_pct"] == 50
    assert d["platform"] == "amazon"
    assert len(d["pros"]) == 2


def test_intent_dispatcher_shopping_reflexes():
    dispatcher = IntentDispatcher()

    # 1. Amazon specific
    r1 = dispatcher.match_instant_reflex("find mechanical keyboard on amazon")
    assert r1 is not None
    assert r1.tier == 1
    assert r1.intent == "shopping"
    assert r1.action == "search_and_compare"
    assert r1.parameters.get("platform") == "amazon"
    assert "mechanical keyboard" in r1.parameters.get("query", "")

    # 2. Flipkart specific
    r2 = dispatcher.match_instant_reflex("search 65w gan charger on flipkart")
    assert r2 is not None
    assert r2.tier == 1
    assert r2.intent == "shopping"
    assert r2.parameters.get("platform") == "flipkart"
    assert "65w gan charger" in r2.parameters.get("query", "")

    # 3. Dual store comparison
    r3 = dispatcher.match_instant_reflex("compare prices of wireless mouse on amazon and flipkart")
    assert r3 is not None
    assert r3.tier == 1
    assert r3.intent == "shopping"
    assert r3.parameters.get("platform") == "both"
    assert "wireless mouse" in r3.parameters.get("query", "")

    # 4. Buy command
    r4 = dispatcher.match_instant_reflex("buy air fryer on amazon")
    assert r4 is not None
    assert r4.tier == 1
    assert r4.intent == "shopping"
    assert r4.parameters.get("platform") == "amazon"

    # 5. Open link commands
    r5 = dispatcher.match_instant_reflex("open it in browser")
    assert r5 is not None
    assert r5.tier == 1
    assert r5.intent == "shopping"
    assert r5.action == "open_item"
    assert r5.parameters.get("mode") == "pc"

    r6 = dispatcher.match_instant_reflex("open the flipkart link")
    assert r6 is not None
    assert r6.tier == 1
    assert r6.intent == "shopping"
    assert r6.action == "open_item"
    assert r6.parameters.get("platform") == "flipkart"


def test_shopping_card_html_generation(tmp_path: Path):
    agent = ShoppingAgent()
    
    amz_item = ShoppingItem(
        title="Amz GaN 65W",
        price=1499.0,
        original_price=2499.0,
        discount_pct=40,
        rating=4.5,
        reviews_count=800,
        platform="amazon",
        url="https://amazon.in/dp/test",
        image_url="",
        item_id="AMZ123",
        badge="Best Seller",
        pros=["Ultra compact"],
        cons=[]
    )
    flp_item = ShoppingItem(
        title="Flp GaN 65W",
        price=1399.0,
        original_price=2299.0,
        discount_pct=39,
        rating=4.2,
        reviews_count=450,
        platform="flipkart",
        url="https://flipkart.com/item/test",
        image_url="",
        item_id="FLP123",
        badge="F-Assured",
        pros=["Value for money"],
        cons=["Slightly larger"]
    )

    synthesis = {
        "verdict": "Flipkart is ₹100 cheaper, but Amazon has higher ratings.",
        "voice_summary": "Amazon has the higher rated model for ₹1,499.",
        "best_overall": amz_item,
        "best_budget": flp_item,
    }

    card_file = agent.generate_hud_shopping_card("65W GaN Charger", synthesis, [amz_item], [flp_item])
    assert Path(card_file).exists()
    content = Path(card_file).read_text(encoding="utf-8")
    assert "Saturn Shopping Intelligence" in content
    assert "Amz GaN 65W" in content
    assert "Flp GaN 65W" in content
    assert "TOP CHOICE" in content
    assert "BEST BUDGET" in content


def test_execute_cart_or_buy():
    agent = ShoppingAgent()

    # PC mode test with mocked webbrowser
    with patch("webbrowser.open") as mock_web:
        res = agent.execute_cart_or_buy("https://amazon.in/dp/test", platform="amazon", mode="pc")
        assert res["success"] is True
        assert res["target"] == "pc"
        mock_web.assert_called_once_with("https://amazon.in/dp/test")

    # Mobile mode test with mocked saturn_connect
    with patch("actions.saturn_connect.connect_execute", return_value='{"success": true}') as mock_connect:
        res_mobile = agent.execute_cart_or_buy("https://flipkart.com/item/test", platform="flipkart", mode="phone")
        assert res_mobile["success"] is True
        assert res_mobile["target"] == "phone"
        mock_connect.assert_called_once()


def test_open_last_item():
    agent = ShoppingAgent()
    item1 = ShoppingItem(title="Item 1", price=100.0, platform="amazon", url="https://amazon.in/dp/1")
    item2 = ShoppingItem(title="Item 2", price=90.0, platform="flipkart", url="https://flipkart.com/dp/2")

    agent.last_results = [item1, item2]
    agent.last_best_pick = item1

    with patch("webbrowser.open") as mock_web:
        # Default opens best pick
        res1 = agent.open_last_item(mode="pc")
        assert res1["success"] is True
        mock_web.assert_called_with("https://amazon.in/dp/1")

        # Specific platform opens that platform item
        res2 = agent.open_last_item(platform="flipkart", mode="pc")
        assert res2["success"] is True
        mock_web.assert_called_with("https://flipkart.com/dp/2")

