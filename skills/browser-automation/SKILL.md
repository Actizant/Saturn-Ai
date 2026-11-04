---
name: browser-automation
description: Autonomous web browsing, Playwright automation, form filling, screenshot verification, and SPA web interaction.
triggers:
  - "open website and click"
  - "automate browser"
  - "fill web form"
  - "capture screenshot of page"
  - "browser test"
---

# Autonomous Browser Automation Skill

## Overview
Control modern web browsers using Playwright to inspect, interact with, test, and extract data from real-world web pages and single-page applications.

## Capabilities & Directives
1. **Navigation & Verification**:
   - Navigate to target URL with wait conditions (`domcontentloaded` or `networkidle`).
   - Capture full-page or element-level screenshots to verify visual state.
2. **Interactive Elements**:
   - Locate elements using resilient selectors: `text=...`, `[data-testid=...]`, `role=button[name='Submit']`.
   - Avoid brittle CSS paths like `div.sc-12345 > span`.
   - Handle dynamic modals, consent cookies, and popups gracefully.
3. **Data Extraction & Form Submission**:
   - Fill inputs with human-like keystroke cadence if bot detection is active.
   - Click buttons and wait for subsequent page transitions or API responses.
   - Extract table data, text hierarchies, or downloadable files.
