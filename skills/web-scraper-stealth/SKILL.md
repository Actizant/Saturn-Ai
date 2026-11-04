---
name: web-scraper-stealth
description: Resilient web scraping, DOM parsing, JSON API reverse engineering, and anti-blocking strategies.
triggers:
  - "scrape website"
  - "extract data from web"
  - "web crawler"
---

# Web Scraper & Data Harvester Skill

## Protocols
1. **Network Inspection First**:
   - Inspect network traffic for internal REST/GraphQL endpoints returning JSON before attempting brittle HTML parsing.
   - Using direct JSON APIs is 10x faster, less resource intensive, and much less fragile.
2. **Resilient DOM Selectors**:
   - Prefer data attributes (`data-testid`, `role`, semantic HTML `<article>`, `<table>`) over generated CSS class names (`css-1a2b3c`).
3. **Anti-Detection Headers**:
   - Real User-Agent, `Accept-Language`, `sec-ch-ua` headers.
   - Respect rate limits with exponential backoff and randomized jitter (1-3s).
4. **Output Formats**:
   - Clean Pandas DataFrame, CSV export, or structured JSON.
