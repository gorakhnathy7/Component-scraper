"""Scrape a site's homepage, pricing page and about page via Firecrawl.

- Homepage: full page (nav + footer kept) because they carry product lists,
  certifications, addresses and the links we need to find the other pages.
- Pricing / About: main content only, since their nav/footer duplicate the homepage.
"""

import asyncio
import os
from urllib.parse import urljoin, urlparse

import httpx

FIRECRAWL_URL = "https://api.firecrawl.dev/v2/scrape"
MAX_CHARS_PER_PAGE = 120_000  # ~30k tokens; guards against pathological pages

PAGE_KEYWORDS = {
    "pricing": ["pricing", "plans", "price"],
    "about": ["about-us", "about", "company", "who-we-are", "our-story"],
}
FALLBACK_PATHS = {"pricing": "/pricing", "about": "/about"}


class ScrapeError(Exception):
    pass


async def _firecrawl(client: httpx.AsyncClient, url: str, main_only: bool, with_links: bool = False) -> dict | None:
    """Return Firecrawl's `data` dict, or None if the page couldn't be scraped."""
    formats = ["markdown", "links"] if with_links else ["markdown"]
    resp = await client.post(
        FIRECRAWL_URL,
        headers={"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}"},
        json={"url": url, "formats": formats, "onlyMainContent": main_only, "timeout": 60000},
    )
    if resp.status_code in (401, 402, 429):
        raise ScrapeError(f"Firecrawl error {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        return None
    body = resp.json()
    data = body.get("data") or {}
    status = (data.get("metadata") or {}).get("statusCode", 200)
    if not body.get("success") or status >= 400 or not (data.get("markdown") or "").strip():
        return None
    return data


def _same_site(a: str, b: str) -> bool:
    strip = lambda u: urlparse(u).netloc.lower().removeprefix("www.")
    return strip(a) == strip(b)


def _find_link(links: list[str], base: str, kind: str) -> str | None:
    """Pick the best same-site link whose path contains one of the keywords."""
    candidates = []
    for link in links:
        full = urljoin(base, link)
        if not _same_site(full, base):
            continue
        path = urlparse(full).path.lower().rstrip("/")
        segments = path.split("/")
        for rank, kw in enumerate(PAGE_KEYWORDS[kind]):
            if kw in segments:
                # Prefer earlier keywords, then shorter paths (e.g. /pricing over /blog/pricing-tips)
                candidates.append((rank, len(segments), full.split("#")[0]))
                break
    return min(candidates)[2] if candidates else None


def _page(name: str, data: dict, fallback_url: str) -> dict:
    md = data["markdown"]
    truncated = len(md) > MAX_CHARS_PER_PAGE
    meta = data.get("metadata") or {}
    return {
        "name": name,
        "url": meta.get("url") or meta.get("sourceURL") or fallback_url,
        "title": meta.get("title", ""),
        "markdown": md[:MAX_CHARS_PER_PAGE],
        "truncated": truncated,
    }


async def _scrape_subpage(client, kind: str, home_url: str, links: list[str]) -> dict | None:
    url = _find_link(links, home_url, kind)
    tried = []
    for candidate in [url, urljoin(home_url, FALLBACK_PATHS[kind])]:
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        data = await _firecrawl(client, candidate, main_only=True)
        if data:
            return _page(kind, data, candidate)
    return None


async def scrape_site(url: str) -> dict:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async with httpx.AsyncClient(timeout=90) as client:
        home = await _firecrawl(client, url, main_only=False, with_links=True)
        if not home:
            raise ScrapeError(f"Could not read the homepage at {url}. The site may block scrapers or be unreachable.")
        home_page = _page("homepage", home, url)
        links = home.get("links") or []

        pricing, about = await asyncio.gather(
            _scrape_subpage(client, "pricing", home_page["url"], links),
            _scrape_subpage(client, "about", home_page["url"], links),
        )

    return {"homepage": home_page, "pricing": pricing, "about": about}
