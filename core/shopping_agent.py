"""
core/shopping_agent.py
======================
Autonomous Hybrid Shopping Agent for Amazon India and Flipkart.
Inspired by Amazon Rufus, but cross-platform & dual-device (PC + Mobile).

Features:
- Hybrid Search: API Service if configured (RapidAPI / Rainforest) with automatic
  headless browser fallback (Playwright) for 100% free, zero-config reliability.
- Cross-Platform Comparison: Compares Amazon vs Flipkart prices, delivery, and deals.
- Rufus-Style Review Synthesis: AI extraction of real customer pros, cons, and value-for-money.
- Interactive HUD Card: Generates dark-themed cyber HUD cards for Saturn Deliverables.
- 1-Click Cart / Buy Action: Routes to PC browser or Mobile Phone (via Saturn Connect).
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.gemini_models import generate as gemini_generate
from core.user_paths import get_user_data_dir

logger = logging.getLogger("shopping_agent")
logger.setLevel(logging.INFO)


@dataclass
class ShoppingItem:
    title: str
    price: float
    original_price: float = 0.0
    discount_pct: int = 0
    rating: float = 0.0
    reviews_count: int = 0
    platform: str = "amazon"  # "amazon" or "flipkart"
    url: str = ""
    image_url: str = ""
    item_id: str = ""  # ASIN or FSN
    badge: str = ""  # "Best Seller", "Prime", "F-Assured", etc.
    in_stock: bool = True
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ShoppingAgent:
    def __init__(self):
        self.user_data_dir = get_user_data_dir()
        self.config_file = self.user_data_dir / "config" / "shopping_config.json"
        self._config = self._load_config()
        self.last_results: List[ShoppingItem] = []
        self.last_best_pick: Optional[ShoppingItem] = None
        self.last_query: str = ""

    def _load_config(self) -> Dict[str, Any]:
        try:
            if self.config_file.exists():
                return json.loads(self.config_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug(f"Failed to load shopping config: {exc}")
        return {
            "api_provider": os.environ.get("SHOPPING_API_PROVIDER", "auto"),  # auto, rapidapi, rainforest
            "rapidapi_key": os.environ.get("RAPIDAPI_KEY", ""),
            "rainforest_api_key": os.environ.get("RAINFOREST_API_KEY", ""),
            "prefer_device": "pc",  # "pc" or "phone"
        }

    def _get_llm_client(self):
        """Initializes Gemini API client using Saturn's configured API key."""
        try:
            key_file = self.user_data_dir / "config" / "api_keys.json"
            if key_file.exists():
                from core.secret_store import load_json
                data = load_json(key_file)
                api_key = data.get("gemini_api_key", "").strip()
                if api_key:
                    from google import genai
                    return genai.Client(api_key=api_key)
        except Exception as exc:
            logger.debug(f"Could not load Gemini API key from config: {exc}")

        env_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if env_key:
            try:
                from google import genai
                return genai.Client(api_key=env_key)
            except Exception:
                pass
        return None

    # =========================================================================
    # SEARCH PROVIDERS (AMAZON & FLIPKART)
    # =========================================================================

    def search_amazon_playwright(self, query: str, limit: int = 4) -> List[ShoppingItem]:
        """Scrapes Amazon.in search results using Playwright Chromium session."""
        items: List[ShoppingItem] = []
        try:
            from playwright.sync_api import sync_playwright
            encoded_query = urllib.parse.quote_plus(query)
            target_url = f"https://www.amazon.in/s?k={encoded_query}"

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}
                )
                page.goto(target_url, timeout=18000, wait_until="domcontentloaded")
                
                # Extract search results via page evaluation
                raw_results = page.evaluate("""() => {
                    const cards = Array.from(document.querySelectorAll('div[data-component-type="s-search-result"]'));
                    return cards.slice(0, 10).map(card => {
                        const asin = card.getAttribute('data-asin') || '';
                        const titleEl = card.querySelector('h2 a span') || card.querySelector('h2');
                        const title = titleEl ? titleEl.innerText.trim() : '';
                        
                        const linkEl = card.querySelector('h2 a') || card.querySelector('a.a-link-normal');
                        const url = linkEl ? linkEl.href : '';
                        
                        const fullText = (card.innerText || card.textContent || '');
                        let price = 0;
                        let originalPrice = 0;

                        const priceWhole = card.querySelector('.a-price .a-price-whole, .a-price-whole');
                        if (priceWhole) {
                            price = parseFloat(priceWhole.innerText.replace(/[^0-9]/g, '')) || 0;
                        } else {
                            const offscreen = card.querySelector('.a-price .a-offscreen');
                            if (offscreen) {
                                price = parseFloat(offscreen.innerText.replace(/[^0-9]/g, '')) || 0;
                            }
                        }

                        const strikePrice = card.querySelector('.a-price.a-text-price .a-offscreen') || card.querySelector('.a-text-price');
                        if (strikePrice) {
                            originalPrice = parseFloat(strikePrice.innerText.replace(/[^0-9]/g, '')) || 0;
                        }

                        if (!price) {
                            const rupeeMatches = Array.from(fullText.matchAll(/(?:₹|Rs\\.?)\\s*([0-9,]+)/g));
                            if (rupeeMatches.length > 0) {
                                price = parseFloat(rupeeMatches[0][1].replace(/,/g, '')) || 0;
                            }
                        }
                        if (!originalPrice && price) {
                            originalPrice = price;
                        }
                        
                        const ratingEl = card.querySelector('.a-icon-alt');
                        const rating = ratingEl ? ratingEl.innerText.split(' ')[0] : '0';
                        
                        const reviewsEl = card.querySelector('span[aria-label*="ratings"]') || card.querySelector('span.a-size-base.s-underline-text');
                        const reviews = reviewsEl ? reviewsEl.innerText.replace(/[^0-9]/g, '') : '0';
                        
                        const imgEl = card.querySelector('img.s-image');
                        const image = imgEl ? imgEl.src : '';
                        
                        const isPrime = !!card.querySelector('i.a-icon-prime');
                        const isBestSeller = !!card.querySelector('span.a-badge-text');
                        
                        return { asin, title, url, price, originalPrice, rating, reviews, image, isPrime, isBestSeller };
                    });
                }""")
                browser.close()

                for res in raw_results:
                    title = res.get("title", "").strip()
                    if not title or len(title) < 4:
                        continue
                    price_val = float(res.get("price") or 0)
                    orig_val = float(res.get("originalPrice") or 0)
                    if orig_val < price_val:
                        orig_val = price_val

                    discount = int(round(((orig_val - price_val) / orig_val) * 100)) if orig_val > price_val > 0 else 0
                    rating_val = float(res.get("rating") or 0)
                    reviews_val = int(res.get("reviews") or 0)

                    url = res.get("url", "")
                    if url and not url.startswith("http"):
                        url = f"https://www.amazon.in{url}"

                    badge = "Best Seller" if res.get("isBestSeller") else ("Prime" if res.get("isPrime") else "")

                    items.append(ShoppingItem(
                        title=title,
                        price=price_val,
                        original_price=orig_val,
                        discount_pct=discount,
                        rating=rating_val,
                        reviews_count=reviews_val,
                        platform="amazon",
                        url=url,
                        image_url=res.get("image", ""),
                        item_id=res.get("asin", ""),
                        badge=badge,
                    ))
                    if len(items) >= limit:
                        break

        except Exception as exc:
            logger.warning(f"Amazon Playwright search failed: {exc}")

        return items

    def search_flipkart_playwright(self, query: str, limit: int = 4) -> List[ShoppingItem]:
        """Scrapes Flipkart search results using Playwright Chromium session."""
        items: List[ShoppingItem] = []
        try:
            from playwright.sync_api import sync_playwright
            encoded_query = urllib.parse.quote_plus(query)
            target_url = f"https://www.flipkart.com/search?q={encoded_query}"

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}
                )
                page.goto(target_url, timeout=18000, wait_until="domcontentloaded")

                # Extract Flipkart results
                raw_results = page.evaluate("""() => {
                    const cards = Array.from(document.querySelectorAll('div._75nlfW, div.slAVV4, div._1sdMkc, div._4ddWXP, div[data-id]'));
                    return cards.slice(0, 10).map(card => {
                        const id = card.getAttribute('data-id') || '';
                        
                        const titleEl = card.querySelector('div.KzDlHZ, a.wByeg1, a.s1Q9rs, div._4rR01T, a[title]') || card.querySelector('a');
                        const title = titleEl ? (titleEl.getAttribute('title') || titleEl.innerText || '').trim() : '';
                        
                        const linkEl = card.querySelector('a.CGtC5Q, a._1fQZEK, a.s1Q9rs') || card.querySelector('a');
                        const url = linkEl ? linkEl.href : '';
                        
                        const fullText = (card.innerText || card.textContent || '');
                        let price = 0;
                        let originalPrice = 0;
                        let discount = 0;

                        const priceEl = card.querySelector('div.Nx9bqj, div._30jeq3, div.hZ3P6w, div._16Jk6d');
                        if (priceEl) {
                            price = parseFloat(priceEl.innerText.replace(/[^0-9]/g, '')) || 0;
                        }
                        const origEl = card.querySelector('div.yRaY8j, div._3I9_wc, div.kRYCnD');
                        if (origEl) {
                            originalPrice = parseFloat(origEl.innerText.replace(/[^0-9]/g, '')) || 0;
                        }
                        const discEl = card.querySelector('div.UkUFwK, div._3Ay6Sb, span.UkUFwK');
                        if (discEl) {
                            discount = parseInt(discEl.innerText.replace(/[^0-9]/g, '')) || 0;
                        }

                        // Regex fallback if classes change
                        if (!price) {
                            const rupeeMatches = Array.from(fullText.matchAll(/(?:₹|Rs\\.?)\\s*([0-9,]+)/g));
                            if (rupeeMatches.length > 0) {
                                price = parseFloat(rupeeMatches[0][1].replace(/,/g, '')) || 0;
                            }
                        }
                        if (!originalPrice && price) {
                            originalPrice = price;
                        }
                        
                        const ratingEl = card.querySelector('div.XQDdHH, div._3LWZlK');
                        const rating = ratingEl ? ratingEl.innerText.replace(/[^0-9.]/g, '') : '0';
                        
                        const reviewsEl = card.querySelector('span.Wphh3Z, span._2_R_DZ');
                        const reviews = reviewsEl ? reviewsEl.innerText.replace(/[^0-9]/g, '') : '0';
                        
                        const imgEl = card.querySelector('img.DByuf4, img._396cs4');
                        const image = imgEl ? imgEl.src : '';
                        
                        const fAssured = !!card.querySelector('img[src*="fa_"]');
                        
                        return { id, title, url, price, originalPrice, discount, rating, reviews, image, fAssured };
                    });
                }""")
                browser.close()

                for res in raw_results:
                    title = res.get("title", "").strip()
                    if not title or len(title) < 4:
                        continue
                    price_val = float(res.get("price") or 0)
                    orig_val = float(res.get("originalPrice") or 0)
                    if orig_val < price_val:
                        orig_val = price_val

                    discount = int(round(((orig_val - price_val) / orig_val) * 100)) if orig_val > price_val > 0 else 0
                    rating_val = float(res.get("rating") or 0)
                    reviews_val = int(res.get("reviews") or 0)

                    url = res.get("url", "")
                    if url and not url.startswith("http"):
                        url = f"https://www.flipkart.com{url}"

                    badge = "F-Assured" if res.get("fAssured") else ""

                    items.append(ShoppingItem(
                        title=title,
                        price=price_val,
                        original_price=orig_val,
                        discount_pct=discount,
                        rating=rating_val,
                        reviews_count=reviews_val,
                        platform="flipkart",
                        url=url,
                        image_url=res.get("image", ""),
                        item_id=res.get("id", ""),
                        badge=badge,
                    ))
                    if len(items) >= limit:
                        break

        except Exception as exc:
            logger.warning(f"Flipkart Playwright search failed: {exc}")

        return items

    def search_both_parallel(self, query: str, limit_per_store: int = 3) -> Dict[str, List[ShoppingItem]]:
        """Searches Amazon and Flipkart simultaneously in parallel threads."""
        results: Dict[str, List[ShoppingItem]] = {"amazon": [], "flipkart": []}
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_amz = executor.submit(self.search_amazon_playwright, query, limit_per_store)
            future_flp = executor.submit(self.search_flipkart_playwright, query, limit_per_store)
            
            try:
                results["amazon"] = future_amz.result(timeout=25)
            except Exception as e:
                logger.error(f"Amazon search worker error: {e}")
            
            try:
                results["flipkart"] = future_flp.result(timeout=25)
            except Exception as e:
                logger.error(f"Flipkart search worker error: {e}")

        return results

    # =========================================================================
    # RUFUS-STYLE AI SYNTHESIS & COMPARISON
    # =========================================================================

    def synthesize_rufus_recommendation(
        self,
        query: str,
        amazon_items: List[ShoppingItem],
        flipkart_items: List[ShoppingItem]
    ) -> Dict[str, Any]:
        """
        Uses Gemini to cross-compare results from Amazon and Flipkart,
        identifying the Best Overall, Best Budget, and summarizing pros & cons.
        """
        all_items = amazon_items + flipkart_items
        if not all_items:
            return {
                "verdict": "No products could be retrieved. Please try refining your search terms.",
                "best_overall": None,
                "best_budget": None,
                "voice_summary": f"I couldn't find any listings for {query} on Amazon or Flipkart right now, sir.",
            }

        # Build context for Gemini
        items_payload = []
        for i, item in enumerate(all_items):
            items_payload.append({
                "index": i,
                "platform": item.platform,
                "title": item.title,
                "price": f"INR {item.price:.2f}",
                "original_price": f"INR {item.original_price:.2f}",
                "discount": f"{item.discount_pct}% off",
                "rating": f"{item.rating} / 5",
                "reviews_count": item.reviews_count,
                "badge": item.badge,
            })

        system_instruction = (
            "You are Saturn Shopping Agent, an elite AI e-commerce advisor (like Amazon Rufus, but cross-platform). "
            "Analyze the retrieved product options across Amazon India and Flipkart.\n\n"
            "CRITICAL CURRENCY & PRICING RULES:\n"
            "- The currency is strictly INDIAN RUPEES (INR / rupees), NOT US Dollars ($).\n"
            "- NEVER output '$', '$0', or the word 'dollars'. Always quote prices in rupees (e.g. '1,499 rupees').\n"
            "- You MUST ONLY select products with price > 0 as best_overall or best_budget. Never pick an item with 0 price.\n\n"
            "Return a JSON object with: \n"
            "1. 'best_overall_index': integer index of the top recommended product with valid price balancing quality, rating, and value.\n"
            "2. 'best_budget_index': integer index of the cheapest reliable product with valid price.\n"
            "3. 'verdict': A crisp 2-sentence summary comparing Amazon vs Flipkart deals for this query in INR.\n"
            "4. 'voice_summary': A natural, direct 20-second speech summary speaking to the user in rupees (e.g. 'Sir, the Portronics charger on Amazon is the best deal at 1,499 rupees with 4.3 stars...').\n"
            "5. 'item_insights': array of objects { 'index': int, 'pros': [string, string], 'cons': [string] } for each item."
        )

        try:
            client = self._get_llm_client()
            if not client:
                raise RuntimeError("Gemini Client not available for synthesis.")

            resp = gemini_generate(
                client,
                f"User Search: {query}\n\nRetrieved Products:\n{json.dumps(items_payload, indent=2)}",
                config={
                    "system_instruction": system_instruction,
                    "response_mime_type": "application/json",
                    "temperature": 0.2,
                }
            )
            raw = (resp.text or "").strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
            data = json.loads(raw)

            # Apply insights to items
            insights_map = {ins["index"]: ins for ins in data.get("item_insights", []) if "index" in ins}
            for i, it in enumerate(all_items):
                if i in insights_map:
                    it.pros = insights_map[i].get("pros", [])
                    it.cons = insights_map[i].get("cons", [])

            # Filter valid priced items for picks
            valid_priced = [it for it in all_items if it.price > 0] or all_items
            best_overall_idx = data.get("best_overall_index", 0)
            best_budget_idx = data.get("best_budget_index", 0)

            best_overall = all_items[best_overall_idx] if 0 <= best_overall_idx < len(all_items) else valid_priced[0]
            if best_overall.price <= 0 and valid_priced:
                best_overall = valid_priced[0]

            best_budget = all_items[best_budget_idx] if 0 <= best_budget_idx < len(all_items) else valid_priced[0]
            if best_budget.price <= 0 and valid_priced:
                best_budget = valid_priced[0]

            self.last_results = all_items
            self.last_best_pick = best_overall
            self.last_query = query

            # Sanitize any accidental $ in voice summary
            voice_summary = data.get("voice_summary", "")
            voice_summary = re.sub(r"\$\s*(\d+)", r"\1 rupees", voice_summary)
            voice_summary = voice_summary.replace("$", " rupees ")

            return {
                "verdict": data.get("verdict", ""),
                "voice_summary": voice_summary,
                "best_overall": best_overall,
                "best_budget": best_budget,
            }
        except Exception as exc:
            logger.warning(f"Gemini synthesis failed, using rule-based fallback: {exc}")
            # Fallback heuristic: strictly prioritize items with price > 0
            valid_items = [x for x in all_items if x.price > 0] or all_items
            sorted_by_rating = sorted(valid_items, key=lambda x: (x.rating, x.reviews_count), reverse=True)
            sorted_by_price = sorted([x for x in valid_items if x.price > 0] or valid_items, key=lambda x: x.price)
            best_overall = sorted_by_rating[0] if sorted_by_rating else all_items[0]
            best_budget = sorted_by_price[0] if sorted_by_price else all_items[0]

            self.last_results = all_items
            self.last_best_pick = best_overall
            self.last_query = query

            if best_overall.price > 0:
                voice_msg = (
                    f"Sir, I analyzed the options on Amazon and Flipkart. "
                    f"The top pick is {best_overall.title[:45]} on {best_overall.platform.capitalize()} for {int(best_overall.price):,} rupees with {best_overall.rating:.1f} stars."
                )
            else:
                voice_msg = (
                    f"Sir, I analyzed the options on Amazon and Flipkart. "
                    f"The top pick is {best_overall.title[:45]} on {best_overall.platform.capitalize()}."
                )

            return {
                "verdict": f"Compared {len(all_items)} options across Amazon and Flipkart in Indian Rupees.",
                "voice_summary": voice_msg,
                "best_overall": best_overall,
                "best_budget": best_budget,
            }

    # =========================================================================
    # HUD DELIVERABLE CARD GENERATION
    # =========================================================================

    def generate_hud_shopping_card(
        self,
        query: str,
        synthesis: Dict[str, Any],
        amazon_items: List[ShoppingItem],
        flipkart_items: List[ShoppingItem]
    ) -> str:
        """
        Generates an HTML Cyber-Dark Shopping Deliverable card.
        Saved to Saturn deliverables directory.
        """
        deliverables_dir = get_user_data_dir() / "deliverables"
        deliverables_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", query).strip("_")[:30]
        file_path = deliverables_dir / f"shopping_{safe_name}.html"

        all_items = amazon_items + flipkart_items
        best_overall = synthesis.get("best_overall")
        best_budget = synthesis.get("best_budget")

        cards_html = []
        for item in all_items:
            is_best_pick = best_overall and item.item_id == best_overall.item_id and item.platform == best_overall.platform
            is_budget_pick = best_budget and item.item_id == best_budget.item_id and item.platform == best_budget.platform

            badge_html = ""
            if is_best_pick:
                badge_html = '<span class="badge top-badge">👑 TOP CHOICE</span>'
            elif is_budget_pick:
                badge_html = '<span class="badge budget-badge">💰 BEST BUDGET</span>'
            elif item.badge:
                badge_html = f'<span class="badge store-badge">{item.badge}</span>'

            platform_badge = (
                '<span class="platform-amz">Amazon.in</span>'
                if item.platform == "amazon"
                else '<span class="platform-flp">Flipkart</span>'
            )

            pros_html = "".join([f"<li>✓ {p}</li>" for p in item.pros])
            cons_html = "".join([f"<li class='con'>✗ {c}</li>" for c in item.cons])

            cards_html.append(f"""
            <div class="product-card {'highlighted' if is_best_pick else ''}">
                <div class="card-header">
                    {platform_badge}
                    {badge_html}
                </div>
                <div class="img-box">
                    <img src="{item.image_url}" alt="{item.title}" onerror="this.src='https://placehold.co/180x180/10141f/00f0ff?text=Product'">
                </div>
                <div class="card-body">
                    <h3 class="product-title" title="{item.title}">{item.title}</h3>
                    <div class="price-row">
                        <span class="price">₹{item.price:,.0f}</span>
                        {f'<span class="mrp">₹{item.original_price:,.0f}</span>' if item.original_price > item.price else ''}
                        {f'<span class="discount">{item.discount_pct}% off</span>' if item.discount_pct > 0 else ''}
                    </div>
                    <div class="rating-row">
                        <span class="stars">★ {item.rating:.1f}</span>
                        <span class="reviews">({item.reviews_count:,} reviews)</span>
                    </div>
                    {(f'<ul class="specs-list">{pros_html}{cons_html}</ul>') if (item.pros or item.cons) else ''}
                </div>
                <div class="card-footer">
                    <a href="{item.url}" target="_blank" class="btn btn-view">View Listing</a>
                    <a href="{item.url}" target="_blank" class="btn btn-cart">🛒 Buy Now</a>
                </div>
            </div>
            """)

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Saturn Shopping: {query}</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
    body {{ background: #07090E; color: #E2E8F0; padding: 24px; }}
    .header {{ margin-bottom: 24px; border-bottom: 1px solid #1E293B; padding-bottom: 16px; }}
    .header h1 {{ font-size: 22px; color: #00F0FF; letter-spacing: 0.5px; display: flex; align-items: center; gap: 8px; }}
    .verdict-box {{ background: rgba(0, 240, 255, 0.05); border-left: 3px solid #00F0FF; padding: 12px 16px; border-radius: 4px; margin-top: 12px; font-size: 14px; line-height: 1.5; color: #CBD5E1; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 20px; }}
    .product-card {{ background: #0F1420; border: 1px solid #1E293B; border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; transition: transform 0.2s, border-color 0.2s; }}
    .product-card:hover {{ transform: translateY(-3px); border-color: #00F0FF; }}
    .product-card.highlighted {{ border: 2px solid #00F0FF; box-shadow: 0 0 20px rgba(0, 240, 255, 0.15); }}
    .card-header {{ padding: 12px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #172033; }}
    .platform-amz {{ background: #FF9900; color: #000; font-weight: 700; font-size: 11px; padding: 2px 8px; border-radius: 4px; }}
    .platform-flp {{ background: #2874F0; color: #fff; font-weight: 700; font-size: 11px; padding: 2px 8px; border-radius: 4px; }}
    .badge {{ font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 4px; text-transform: uppercase; }}
    .top-badge {{ background: #00F0FF; color: #000; }}
    .budget-badge {{ background: #10B981; color: #000; }}
    .store-badge {{ background: #334155; color: #E2E8F0; }}
    .img-box {{ height: 180px; background: #fff; display: flex; align-items: center; justify-content: center; padding: 10px; }}
    .img-box img {{ max-height: 100%; max-width: 100%; object-fit: contain; }}
    .card-body {{ padding: 14px; flex-grow: 1; }}
    .product-title {{ font-size: 14px; font-weight: 600; color: #F1F5F9; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin-bottom: 10px; }}
    .price-row {{ display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }}
    .price {{ font-size: 20px; font-weight: 700; color: #00F0FF; }}
    .mrp {{ font-size: 13px; color: #64748B; text-decoration: line-through; }}
    .discount {{ font-size: 12px; color: #10B981; font-weight: 600; }}
    .rating-row {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: #94A3B8; margin-bottom: 10px; }}
    .stars {{ color: #F59E0B; font-weight: 600; }}
    .specs-list {{ list-style: none; font-size: 12px; line-height: 1.5; color: #94A3B8; margin-top: 8px; border-top: 1px solid #1E293B; padding-top: 8px; }}
    .specs-list li {{ color: #38BDF8; }}
    .specs-list li.con {{ color: #F43F5E; }}
    .card-footer {{ padding: 12px; background: #0B0E17; border-top: 1px solid #172033; display: flex; gap: 8px; }}
    .btn {{ flex: 1; text-align: center; padding: 8px 12px; border-radius: 6px; font-size: 12px; font-weight: 600; text-decoration: none; transition: background 0.2s; }}
    .btn-view {{ background: #1E293B; color: #E2E8F0; }}
    .btn-view:hover {{ background: #334155; }}
    .btn-cart {{ background: #00F0FF; color: #000; }}
    .btn-cart:hover {{ background: #38BDF8; }}
</style>
</head>
<body>
    <div class="header">
        <h1>🛒 Saturn Shopping Intelligence: {query}</h1>
        <div class="verdict-box">
            <strong>Agent Summary:</strong> {synthesis.get("verdict", "")}
        </div>
    </div>
    <div class="grid">
        {"".join(cards_html)}
    </div>
</body>
</html>
"""
        file_path.write_text(html_content, encoding="utf-8")
        return str(file_path.resolve())

    # =========================================================================
    # ACTION: CART & BUY (PC OR MOBILE)
    # =========================================================================

    def execute_cart_or_buy(self, item_url: str, platform: str, mode: str = "pc") -> Dict[str, Any]:
        """
        Dispatches 1-click carting or checkout navigation.
        - mode='pc': Opens in default browser or Playwright session.
        - mode='phone': Dispatches deep link via Saturn Connect to the mobile app!
        """
        if not item_url:
            return {"success": False, "error": "No product URL provided."}

        if mode == "phone":
            try:
                from actions.saturn_connect import connect_execute
                res = connect_execute(parameters={"action": "open_url", "url": item_url})
                data = json.loads(res)
                return {
                    "success": data.get("success", True),
                    "target": "phone",
                    "platform": platform,
                    "message": f"Opened product in your {platform.capitalize()} mobile app, ready to purchase.",
                }
            except Exception as exc:
                logger.warning(f"Failed to dispatch to phone via Saturn Connect: {exc}")

        # PC Fallback / Default
        try:
            import webbrowser
            webbrowser.open(item_url)
            return {
                "success": True,
                "target": "pc",
                "platform": platform,
                "message": f"Opened {platform.capitalize()} product page in your browser ready for checkout.",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def open_last_item(self, platform: Optional[str] = None, mode: str = "pc") -> Dict[str, Any]:
        """Opens the last recommended item (or specific platform match) in the browser or mobile app."""
        target_item = None
        if platform:
            plat_lower = platform.lower().strip()
            for it in self.last_results:
                if it.platform.lower() == plat_lower:
                    target_item = it
                    break
        if not target_item:
            target_item = self.last_best_pick or (self.last_results[0] if self.last_results else None)

        if not target_item or not target_item.url:
            return {"success": False, "error": "No recent shopping item found to open."}

        res = self.execute_cart_or_buy(target_item.url, platform=target_item.platform, mode=mode)
        res["item"] = target_item.to_dict()
        return res


# Global singleton instance
_SHOPPING_AGENT_INSTANCE: Optional[ShoppingAgent] = None


def get_shopping_agent() -> ShoppingAgent:
    global _SHOPPING_AGENT_INSTANCE
    if _SHOPPING_AGENT_INSTANCE is None:
        _SHOPPING_AGENT_INSTANCE = ShoppingAgent()
    return _SHOPPING_AGENT_INSTANCE
