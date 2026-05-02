"""
Research publication keyword discovery.

Mines actual academic output from the institution across 3 databases:
  1. OpenAlex works  — search by raw affiliation string (works even without entity)
  2. Semantic Scholar — author affiliation search → their papers
  3. CrossRef         — affiliation query on works

Feeds paper titles + topics + abstracts to Claude → grant-optimal keywords.
Runs AFTER web enrichment, BEFORE grant searches.
"""
import asyncio
import json
import logging
import re
import subprocess
from temporalio import activity
import httpx

from ..models import ClientProfileInput

_log = logging.getLogger("grant-agent")

def _info(msg):
    try: activity.logger.info(msg)
    except Exception: _log.info(msg)

def _warn(msg):
    try: activity.logger.warning(msg)
    except Exception: _log.warning(msg)

_OA_HEADERS  = {"User-Agent": "grant-agent/1.0 (research intelligence service)"}
_S2_HEADERS  = {"User-Agent": "grant-agent/1.0"}


@activity.defn
async def discover_research_keywords(profile: ClientProfileInput) -> dict:
    """
    Returns {keywords: [...], topics: [...], research_summary: str}
    or {} if nothing useful found.
    """
    name = profile.name
    # Try both the full name and a shortened version without parenthetical acronym
    short_name = re.sub(r"\s*\([^)]+\)", "", name).strip()
    queries = list(dict.fromkeys([name, short_name]))  # deduplicated, order preserved

    snippets: list[str] = []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        oa, s2, cr = await asyncio.gather(
            _openalex_works(client, queries),
            _semantic_scholar(client, queries),
            _crossref_affiliation(client, queries),
            return_exceptions=True,
        )
        if isinstance(oa, list) and oa:
            snippets.append(f"[OPENALEX WORKS — {len(oa)} papers]\n{oa}")
        if isinstance(s2, list) and s2:
            snippets.append(f"[SEMANTIC SCHOLAR — {len(s2)} papers]\n{s2}")
        if isinstance(cr, list) and cr:
            snippets.append(f"[CROSSREF — {len(cr)} works]\n{cr}")

    if not snippets:
        _warn(f"[research_kw] no publication data found for {name}")
        return {}

    combined = "\n\n".join(str(s) for s in snippets)
    _info(f"[research_kw] {len(snippets)} source(s) found, sending to Claude")

    result = await asyncio.get_event_loop().run_in_executor(
        None, _claude_extract, name, combined, profile.keywords, profile.research_areas
    )
    return result


# ── OpenAlex works by raw affiliation string ─────────────────────────────────

async def _openalex_works(client: httpx.AsyncClient, queries: list[str]) -> list:
    for q in queries:
        try:
            resp = await client.get(
                "https://api.openalex.org/works",
                params={
                    "filter": f"authorships.raw_affiliation_strings.search:{q}",
                    "select": "title,keywords,topics",
                    "per-page": 25,
                    "sort": "cited_by_count:desc",
                },
                headers=_OA_HEADERS,
            )
            works = resp.json().get("results", [])
            if not works:
                continue

            lines = []
            for w in works:
                title = w.get("title", "")
                kws   = [k.get("display_name", "") for k in w.get("keywords", [])]
                tops  = [t.get("display_name", "") for t in w.get("topics",   [])]
                lines.append(f"- {title} | keywords: {', '.join(kws[:5])} | topics: {', '.join(tops[:3])}")

            _info(f"[research_kw] OpenAlex: {len(lines)} works for '{q}'")
            return lines
        except Exception as exc:
            _warn(f"[research_kw] OpenAlex error for '{q}': {exc}")
    return []


# ── Semantic Scholar author → papers ─────────────────────────────────────────

async def _semantic_scholar(client: httpx.AsyncClient, queries: list[str]) -> list:
    for q in queries:
        try:
            resp = await client.get(
                "https://api.semanticscholar.org/graph/v1/author/search",
                params={"query": q, "fields": "name,affiliations,paperCount", "limit": 5},
                headers=_S2_HEADERS,
            )
            authors = resp.json().get("data", [])
            # Keep only authors whose affiliation string mentions the query
            q_lower = q.lower()
            matched = [
                a for a in authors
                if any(q_lower in aff.lower() for aff in a.get("affiliations", []))
            ]
            if not matched:
                matched = authors[:2]  # fallback: take top 2

            lines = []
            for author in matched[:3]:
                aid = author.get("authorId", "")
                if not aid:
                    continue
                pr = await client.get(
                    f"https://api.semanticscholar.org/graph/v1/author/{aid}/papers",
                    params={"fields": "title,abstract,fieldsOfStudy,year", "limit": 6},
                    headers=_S2_HEADERS,
                )
                for p in pr.json().get("data", []):
                    t   = p.get("title", "")
                    fos = ", ".join(p.get("fieldsOfStudy", []))
                    ab  = (p.get("abstract") or "")[:120]
                    lines.append(f"- {t} [{fos}] — {ab}")

            if lines:
                _info(f"[research_kw] Semantic Scholar: {len(lines)} papers for '{q}'")
                return lines
        except Exception as exc:
            _warn(f"[research_kw] Semantic Scholar error for '{q}': {exc}")
    return []


# ── CrossRef affiliation search ───────────────────────────────────────────────

async def _crossref_affiliation(client: httpx.AsyncClient, queries: list[str]) -> list:
    for q in queries:
        try:
            resp = await client.get(
                "https://api.crossref.org/works",
                params={
                    "query.affiliation": q,
                    "rows": 15,
                    "select": "title,subject,abstract",
                    "sort": "is-referenced-by-count",
                    "order": "desc",
                },
            )
            items = resp.json().get("message", {}).get("items", [])
            if not items:
                continue

            lines = []
            for it in items:
                title   = " ".join(it.get("title", []))
                subject = ", ".join(it.get("subject", []))
                ab      = (it.get("abstract") or "")[:100]
                lines.append(f"- {title} | subjects: {subject} | {ab}")

            if lines:
                _info(f"[research_kw] CrossRef: {len(lines)} works for '{q}'")
                return lines
        except Exception as exc:
            _warn(f"[research_kw] CrossRef error for '{q}': {exc}")
    return []


# ── Claude synthesis ──────────────────────────────────────────────────────────

def _claude_extract(
    institution_name: str,
    papers_text: str,
    existing_keywords: list[str],
    existing_areas: list[str],
) -> dict:
    existing_kw_str    = ", ".join(existing_keywords)   or "none"
    existing_areas_str = ", ".join(existing_areas)      or "none"

    prompt = f"""You are a grant research specialist. Based on the academic publications below from "{institution_name}", extract the most specific and grant-searchable keywords for finding relevant funding opportunities.

EXISTING KEYWORDS: {existing_kw_str}
EXISTING RESEARCH AREAS: {existing_areas_str}

ACADEMIC OUTPUT:
{papers_text[:5000]}

Return ONLY a JSON object with:
- "keywords": list of 12–18 highly specific, grant-searchable keywords derived from the actual research (e.g. "magnetic hyperthermia", "perovskite photovoltaics", "CRISPR gene editing", "antibiotic resistance", "microplastic remediation")
  Rules: prefer specific methodologies and research topics over generic terms; include both technical terms AND their grant-friendly equivalents; mix terms that work for Indian funding agencies (SERB, DST, ICMR) AND international ones (NIH, NSF, EU Horizon)
- "topics": list of 5–7 broad research area labels
- "research_summary": 2-sentence description of what this institution actually researches, based on their publications

Merge with and improve on existing keywords — keep the good ones, add new specific ones from publications. Return ONLY the JSON object."""

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=60,
        )
        raw = result.stdout.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e == -1:
            return {}
        data = json.loads(raw[s : e + 1])
        _info(f"[research_kw] Claude extracted {len(data.get('keywords', []))} keywords from publications")
        return data
    except Exception as exc:
        _warn(f"[research_kw] Claude extraction error: {exc}")
        return {}
