"""core/briefing_sources.py - the truth behind the Daily Briefing page.

Pure, Qt-free fetchers that turn the briefing's sample cards into live data.
Every source is keyless (wttr.in, Yahoo Finance, BBC RSS) or local (the
calendar's JSON store, the memory engine), and every fetcher returns None on
any failure so the page can fall back to its mockup rows instead of showing
an empty box. The UI calls these from a worker thread - nothing here may
touch Qt.

Shape contract with DailyBriefingPage (ui.py):
    fetch_weather()  -> dict matching DailyBriefingPage.WX + "city"
    fetch_markets()  -> list[{"label","value","pct","sym"}]
    fetch_calendar() -> list[tuple] matching DailyBriefingPage.EVB
    fetch_news("local"|"world") -> list[dict] matching NL/NW + "url"
    fetch_memory_count() -> int | None
"""

from __future__ import annotations

import datetime
import html as _html
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "Saturn-Briefing/1.0"}
TIMEOUT = 6.0

# The three instruments the Markets card shows. Yahoo answers these keyless.
MARKETS = [("^NSEI", "Nifty 50"), ("^BSESN", "Sensex"), ("BTC-USD", "Bitcoin")]

# Keyless feeds for the news pair: Indian edition of Google News for local,
# BBC World for the planet. (BBC's India feed was retired upstream with a 404.)
FEEDS = {"local": "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
         "world": "https://feeds.bbci.co.uk/news/world/rss.xml"}

# wttr condition word -> the app's _IC_PATHS icon kind (ui.py owns the icons).
_ICON_WORDS = (("rain", "rain"), ("shower", "rain"), ("drizzle", "rain"),
               ("thunder", "rain"), ("snow", "rain"), ("sleet", "rain"),
               ("cloud", "wx"), ("mist", "wx"), ("fog", "wx"), ("haze", "wx"),
               ("clear", "sun"), ("sunny", "sun"))


def _get(url: str, timeout: float = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _cond_icon(desc: str) -> str:
    d = (desc or "").lower()
    for word, icon in _ICON_WORDS:
        if word in d:
            return icon
    return "wx"


# ── Weather ────────────────────────────────────────────────────────────────
def fetch_weather():
    """Live weather + 3-day outlook from wttr.in, honouring the device
    location the same way actions/weather_report.py does. Returns the WX dict
    shape the card paints, or None."""
    city = None
    lat = lon = None
    try:
        from core.device_location import get_device_location
        loc = get_device_location() or {}
        city = loc.get("city")
        lat, lon = loc.get("latitude"), loc.get("longitude")
    except Exception:
        pass
    if lat is not None and lon is not None:
        url = f"https://wttr.in/{lat:.4f},{lon:.4f}?format=j1"
    elif city:
        from urllib.parse import quote_plus
        url = f"https://wttr.in/{quote_plus(city)}?format=j1"
    else:
        url = "https://wttr.in/?format=j1"
    try:
        data = json.loads(_get(url, timeout=4.0).decode("utf-8"))
        cur = (data.get("current_condition") or [{}])[0]
        days = data.get("weather") or []
        area = (data.get("nearest_area") or [{}])[0]
        cname = ((area.get("areaName") or [{}])[0].get("value")) or city or "Your area"
        cond = (cur.get("weatherDesc") or [{}])[0].get("value", "").strip() or "Mixed"
        t = int(cur.get("temp_C", 0))
        today = days[0] if days else {}
        hourly = today.get("hourly") or []
        # Six points across the day, every third slot (wttr gives 8 at 3 h).
        hrs = []
        for slot in hourly[::2][:6] if len(hourly) >= 8 else hourly[:6]:
            hh = str(slot.get("time", "0")).zfill(4)[:2]
            hrs.append((f"{int(hh):02d}", int(slot.get("tempC", t))))
        rain = max((int(s.get("chanceofrain", 0)) for s in hourly), default=0)
        d3 = []
        for d in days[:3]:
            date = datetime.date.fromisoformat(d.get("date"))
            a = (d.get("hourly") or [{}])
            mid = a[len(a) // 2] if a else {}
            d3.append(((["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                        [date.weekday()]),
                       int(d.get("maxtempC", 0)), int(d.get("mintempC", 0)),
                       _cond_icon((mid.get("weatherDesc") or [{}])[0]
                                  .get("value", ""))))
        return {"city": cname, "c": cond, "t": t,
                "fl": int(cur.get("FeelsLikeC", t)),
                "hi": int(today.get("maxtempC", t)),
                "lo": int(today.get("mintempC", t)),
                "rain": rain, "wind": int(cur.get("windspeedKmph", 0)),
                "hum": int(cur.get("humidity", 0)),
                "hrs": hrs or [("Now", t)], "d3": d3}
    except Exception:
        return None


# ── Markets ────────────────────────────────────────────────────────────────
def _fetch_one_market(sym: str, label: str):
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
               "?range=1d&interval=5m")
        data = json.loads(_get(url).decode("utf-8"))
        meta = (data.get("chart", {}).get("result") or [{}])[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev = (meta.get("chartPreviousClose") or meta.get("previousClose")
                or meta.get("regularMarketPreviousClose"))
        if price is None:
            return None
        pct = ((price - prev) / prev * 100.0) if prev else 0.0
        if sym.startswith("BTC"):
            value = f"{price:,.0f}"
        else:
            value = f"{price:,.2f}"
        return {"label": label, "sym": sym, "value": value, "pct": round(pct, 2)}
    except Exception:
        return None


def fetch_markets():
    """Nifty / Sensex / Bitcoin in parallel; [] when none answered."""
    try:
        with ThreadPoolExecutor(max_workers=len(MARKETS)) as pool:
            rows = list(pool.map(
                lambda m: _fetch_one_market(m[0], m[1]), MARKETS))
        return [r for r in rows if r] or None
    except Exception:
        return None


# ── Calendar ───────────────────────────────────────────────────────────────
def fetch_calendar():
    """Today's scheduled events from the app's own calendar store, in the
    EVB tuple shape (start_h, start_m, end_h, end_m, title)."""
    try:
        from actions.calendar_scheduler import _load_events
        today = datetime.date.today().isoformat()
        rows = []
        for ev in _load_events():
            if str(ev.get("date", "")) != today:
                continue
            m = re.match(r"(\d{1,2}):(\d{2})", str(ev.get("time", "")))
            if not m:
                continue
            sh, sm = int(m.group(1)), int(m.group(2))
            mins = sh * 60 + sm + int(ev.get("duration_minutes") or 30)
            rows.append((sh, sm, (mins // 60) % 24, mins % 60,
                         str(ev.get("title") or "Event")))
        rows.sort(key=lambda r: (r[0], r[1]))
        return rows or []
    except Exception:
        return None


# ── News ───────────────────────────────────────────────────────────────────
def _ago(pubdate: str) -> str:
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pubdate)
        mins = int((datetime.datetime.now(dt.tzinfo) - dt).total_seconds() // 60)
        if mins < 60:
            return f"{max(1, mins)} min ago"
        if mins < 24 * 60:
            return f"{mins // 60} h ago"
        return f"{mins // (24 * 60)} d ago"
    except Exception:
        return "today"


def fetch_news(which: str = "world"):
    """Four RSS items from the BBC feed for local/world, in the NL/NW dict
    shape plus the article url. The feed is read with scoped regexes rather
    than a general XML parser: RSS arrives over the network, and a stock
    parser's entity/external-resource handling is an attack surface this
    page does not need."""
    url = FEEDS.get(which)
    if not url:
        return None

    def field(block: str, name: str) -> str:
        m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
        if not m:
            return ""
        raw = m.group(1).strip()
        raw = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", raw, flags=re.S)
        return _html.unescape(raw)

    try:
        text = _get(url, timeout=5.0).decode("utf-8", "replace")
        items = []
        for block in re.findall(r"<item[ >].*?</item>", text, re.S)[:8]:
            title = field(block, "title")
            if not title:
                continue
            desc = re.sub(r"<[^>]+>", "", field(block, "description")).strip()
            src = field(block, "source")
            # Google News bakes " - Source" into the title; the card shows
            # the source separately, so trim the duplication.
            if src and title.endswith(f" - {src}"):
                title = title[: -len(f" - {src}")].strip()
            items.append({"h": title, "s": desc[:140] or title,
                          "src": src or "BBC News",
                          "ago": _ago(field(block, "pubDate")),
                          "url": field(block, "link")})
            if len(items) >= 4:
                break
        return items or None
    except Exception:
        return None


# ── Memory ─────────────────────────────────────────────────────────────────
def fetch_memory_count():
    try:
        from memory.memory_manager import all_entries_for_ui
        return len(all_entries_for_ui() or [])
    except Exception:
        return None
