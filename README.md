# Website Q&A demo

Paste a product website URL. The app scrapes its homepage, pricing page and about page with Firecrawl,
has Claude (Sonnet 5.5) summarize them, then answers follow-up questions using only those three pages.

**Component Lab** (second tab after a site is loaded): share a screenshot of an animation or visual you like on
that site. A headless browser finds it on the homepage, records how it moves, and extracts its markup, CSS,
keyframes, running animations and libraries. Claude rebuilds it as a standalone HTML component, renders the rebuild,
compares it with the original, and fixes it once if needed. You then play with it in a sandboxed iframe (sliders
generated from the component's parameters, pause/scrub/speed, viewport widths, chat edits with undo) and can copy or
download it. Lottie animations are played from the site's original file rather than redrawn.

## Setup

```bash
cp .env.example .env          # then fill in ANTHROPIC_API_KEY and FIRECRAWL_API_KEY
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/playwright install chromium   # headless browser for Component Lab
.venv/bin/uvicorn backend.main:app --reload
```

Open http://localhost:8000

## Layout

- `backend/scraper.py` – Firecrawl calls; finds pricing/about links on the homepage (falls back to `/pricing`, `/about`)
- `backend/prompts.py` – grounding rules, summary prompt, suggestion format
- `backend/llm.py` – builds the cached system prompt, calls Claude, trims history
- `backend/main.py` – `POST /ingest`, `POST /chat`, `POST /component/{locate,select,build,edit}`, serves the frontend
- `backend/browser.py` – Playwright: open the homepage, match the screenshot to an element, record frames, render rebuilds
- `backend/page_tools.js` – runs inside the page: element matching, CSS/animation/library extraction
- `backend/component.py` – Component Lab pipeline: locate → capture → rebuild → compare → fix, plus edits
- `frontend/index.html` – URL screen → Chat / Component Lab tabs (plain HTML/JS)

Demo tip: https://juspay.io works well. Screenshot the animated circuit graphic in the hero and paste it into
Component Lab; it finds the graphic, plays the original Lottie, and edits like "make the logo purple" work.

The server terminal prints token usage per Claude call; `cache_read` > 0 on follow-ups means prompt caching is working.
