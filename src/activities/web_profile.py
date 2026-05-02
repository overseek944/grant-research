"""
Web-based profile enrichment — no API key needed.
Strategy:
  1. If profile.url is set, try to fetch and scrape it directly.
  2. Always run 2 DuckDuckGo HTML searches: research areas + grants/funding history.
  3. Feed collected text to Claude → extract grant-optimal keywords and research areas.

Works even when institutions are too small for OpenAlex coverage.
"""
import asyncio
import json
import re
import subprocess
import urllib.parse
import httpx
import logging
from bs4 import BeautifulSoup
from temporalio import activity
from ..models import ClientProfileInput

_log = logging.getLogger("grant-agent")

def _info(msg):
    try: activity.logger.info(msg)
    except Exception: _log.info(msg)

def _warn(msg):
    try: activity.logger.warning(msg)
    except Exception: _log.warning(msg)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_DDG_URL = "https://html.duckduckgo.com/html/"


@activity.defn
async def enrich_profile_from_web(profile: ClientProfileInput) -> dict:
    """
    Returns {keywords: [...], research_areas: [...], web_summary: str}
    or {} if enrichment fails / adds nothing useful.
    """
    name = profile.name
    snippets: list[str] = []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": _UA}) as client:

        # ── Step 1: scrape the programme URL if provided ──────────────────
        if profile.url:
            page_text = await _fetch_page(client, profile.url)
            if page_text:
                snippets.append(f"[FROM PROGRAMME PAGE]\n{page_text[:3000]}")
                _info(f"[web_profile] scraped programme page: {len(page_text)} chars")
            else:
                _info(f"[web_profile] programme page blocked/empty, falling back to search")

        # ── Step 2: DuckDuckGo — research areas ──────────────────────────
        q1 = f'"{name}" research areas focus projects faculty'
        results1 = await _ddg_search(client, q1)
        if results1:
            snippets.append(f"[SEARCH: research areas]\n{results1}")

        # ── Step 3: DuckDuckGo — grants and funding ──────────────────────
        q2 = f'"{name}" grants funding sponsored research external collaboration'
        results2 = await _ddg_search(client, q2)
        if results2:
            snippets.append(f"[SEARCH: funding history]\n{results2}")

        # ── Step 4: DuckDuckGo — programme-specific if URL given ─────────
        if profile.url:
            domain = urllib.parse.urlparse(profile.url).netloc
            q3 = f"site:{domain} research programme projects"
            results3 = await _ddg_search(client, q3)
            if results3:
                snippets.append(f"[SEARCH: site-specific]\n{results3}")

    if not snippets:
        _warn(f"[web_profile] no web data collected for {name}")
        return {}

    combined = "\n\n".join(snippets)
    _info(f"[web_profile] collected {len(combined)} chars from web, sending to Claude")

    result = await asyncio.get_event_loop().run_in_executor(
        None, _claude_extract_keywords, name, combined,
        profile.keywords, profile.research_areas
    )
    return result


async def _fetch_page(client: httpx.AsyncClient, url: str) -> str:
    """Fetch and clean a programme page. Returns plain text or empty string."""
    try:
        resp = await client.get(url, timeout=15)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        # Remove nav/header/footer noise
        for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
            tag.decompose()
        # Focus on content-bearing tags
        content_tags = soup.find_all(["main", "article", "section", "div"])
        if content_tags:
            text = " ".join(t.get_text(" ", strip=True) for t in content_tags[:10])
        else:
            text = soup.get_text(" ", strip=True)
        # Collapse whitespace
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text[:4000]
    except Exception as exc:
        _warn(f"[web_profile] fetch error {url}: {exc}")
        return ""


async def _ddg_search(client: httpx.AsyncClient, query: str) -> str:
    """Search DuckDuckGo HTML and return concatenated snippets."""
    try:
        resp = await client.post(
            _DDG_URL,
            data={"q": query, "b": "", "kl": ""},
            headers={"User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if resp.status_code != 200:
            # Try GET fallback
            resp = await client.get(
                _DDG_URL,
                params={"q": query},
                headers={"User-Agent": _UA},
                timeout=15,
            )
        soup = BeautifulSoup(resp.text, "lxml")
        snippets = []
        for el in soup.select(".result__snippet, .result__body, .result__title")[:12]:
            t = el.get_text(" ", strip=True)
            if len(t) > 30:
                snippets.append(t)
        result = " | ".join(snippets)
        _info(f"[web_profile] DDG '{query[:60]}': {len(snippets)} snippets")
        return result[:2000]
    except Exception as exc:
        _warn(f"[web_profile] DDG search error: {exc}")
        return ""


def _claude_extract_keywords(
    institution_name: str,
    web_text: str,
    existing_keywords: list[str],
    existing_areas: list[str],
) -> dict:
    """Ask Claude to extract the best grant-matching keywords from web content."""
    existing_kw_str = ", ".join(existing_keywords) if existing_keywords else "none"
    existing_areas_str = ", ".join(existing_areas) if existing_areas else "none"

    prompt = f"""You are a grant research specialist. Based on web search results about "{institution_name}", extract the best keywords for finding relevant grant opportunities.

EXISTING KEYWORDS (from YAML): {existing_kw_str}
EXISTING RESEARCH AREAS (from YAML): {existing_areas_str}

WEB CONTENT:
{web_text[:4000]}

Return ONLY a JSON object with these fields:
- "keywords": list of 12–15 specific, grant-searchable keywords (e.g. "nanoparticles", "urban health", "machine learning", "environmental monitoring", "infectious disease")
  Rules: prefer specific technical terms over generic ones; include institution's actual research topics; mix Indian govt-friendly terms with international ones
- "research_areas": list of 5–7 broad research area strings (e.g. "computational biology", "earth and environmental sciences")
- "web_summary": 2-sentence summary of what this institution/programme actually does, based on the web content

Merge and improve on the existing keywords — keep good ones, add new ones discovered from web content. Return ONLY the JSON object."""

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=60,
        )
        raw = result.stdout.strip()
        # Strip markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return {}
        data = json.loads(raw[start : end + 1])
        _info(f"[web_profile] Claude extracted {len(data.get('keywords', []))} keywords")
        return data
    except Exception as exc:
        _warn(f"[web_profile] Claude extraction error: {exc}")
        return {}
