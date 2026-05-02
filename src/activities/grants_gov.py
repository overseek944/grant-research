"""
Grants.gov – US federal grant opportunities.
Scrapes Grants.gov search + curated active federal calls as reliable fallback.
No API key required.
"""
import logging
from typing import Optional
import httpx
from bs4 import BeautifulSoup
from temporalio import activity
from ..models import GrantInput, ClientProfileInput

_log = logging.getLogger("grant-agent")

def _info(msg):
    try: activity.logger.info(msg)
    except Exception: _log.info(msg)

def _warn(msg):
    try: activity.logger.warning(msg)
    except Exception: _log.warning(msg)


# Curated major active/rolling US federal programmes
ACTIVE_FEDERAL_CALLS = [
    {
        "id": "nih-r01-rolling",
        "title": "NIH R01 Research Project Grant (Standard Dates)",
        "agency": "NIH (National Institutes of Health)",
        "description": "The R01 is NIH's flagship research grant, supporting hypothesis-driven research in any biomedical or behavioral science area. Awarded for up to 5 years. New and early-stage investigators receive special review consideration.",
        "funding_amount": "Up to $250,000 direct costs/year (standard); higher for modular grants",
        "deadline": "2025-10-05",
        "eligibility": "US and international institutions; PI must have PhD or equivalent",
        "url": "https://grants.nih.gov/grants/funding/r01.htm",
    },
    {
        "id": "nih-r21-rolling",
        "title": "NIH R21 Exploratory/Developmental Research Grant",
        "agency": "NIH",
        "description": "Supports new, exploratory, and developmental research projects. Ideal for pilot data collection before a larger R01. Up to 2 years and ,000 total.",
        "funding_amount": "Up to $275,000 total (2 years)",
        "deadline": "2025-10-16",
        "eligibility": "Any US or international institution; no preliminary data required",
        "url": "https://grants.nih.gov/grants/funding/r21.htm",
    },
    {
        "id": "fogarty-d43-2025",
        "title": "Fogarty International Center – D43 Global Health Research Training",
        "agency": "NIH Fogarty International Center",
        "description": "Supports research training programs in LMIC countries including India. Funds US-LMIC institutional partnerships for building research capacity in infectious diseases, global health, and NCDs.",
        "funding_amount": "$250,000–$500,000/year for 5 years",
        "deadline": "2025-11-14",
        "eligibility": "US institution with LMIC partner; Indian institutions regularly serve as LMIC partner",
        "url": "https://www.fic.nih.gov/Programs/Pages/research-training-grants.aspx",
    },
    {
        "id": "sbir-sttr-nih-2025",
        "title": "NIH SBIR/STTR Phase I & II (Health Technologies)",
        "agency": "NIH – Small Business Programs",
        "description": "Funds US small businesses to develop biomedical technologies from concept to commercialization. Phase I: up to $400K. Phase II: up to $3M. High relevance for medtech, diagnostics, digital health startups.",
        "funding_amount": "Phase I: up to $400,000 | Phase II: up to $3,000,000",
        "deadline": "2025-09-05",
        "eligibility": "US small businesses (≤500 employees); foreign collaboration allowed in STTR",
        "url": "https://seed.nih.gov/",
    },
    {
        "id": "nsf-global-centers-2025",
        "title": "NSF Global Centers – Convergence Research with International Collaboration",
        "agency": "NSF (National Science Foundation)",
        "description": "NSF Global Centers funds large convergence research projects between US and international institutions. India-US partnerships are explicitly encouraged. Focus areas include climate, health, and food security.",
        "funding_amount": "Up to $5,000,000 over 5 years",
        "deadline": "2025-12-01",
        "eligibility": "US institution as lead; Indian partner institutions (IITs, IISc, AIIMS) eligible as international collaborators",
        "url": "https://www.nsf.gov/pubs/2023/nsf23565/nsf23565.htm",
    },
    {
        "id": "usaid-dev-innovation-2025",
        "title": "USAID Development Innovation Ventures (DIV)",
        "agency": "USAID – Bureau for Development, Democracy, and Innovation",
        "description": "Provides tiered funding for innovative solutions to global development challenges. Stage 1: $25K–$200K for early pilots. Stage 2: $200K–$1.5M for rigorous testing. Open to NGOs, social enterprises, and research institutions globally.",
        "funding_amount": "$25,000–$1,500,000 depending on stage",
        "deadline": "Rolling",
        "eligibility": "Global — NGOs, social enterprises, universities in any country including India",
        "url": "https://www.usaid.gov/div",
    },
]


@activity.defn
async def search_grants_gov(profile: ClientProfileInput, max_results: int = 30) -> list[GrantInput]:
    grants: list[GrantInput] = []
    kw_lower = [k.lower() for k in profile.keywords + profile.research_areas]

    # Include curated active federal grants filtered by relevance
    for call in ACTIVE_FEDERAL_CALLS:
        desc_lower = call["description"].lower() + " " + call["title"].lower()
        if not kw_lower or any(k in desc_lower for k in kw_lower):
            grants.append(GrantInput(
                id=call["id"],
                title=call["title"],
                agency=call["agency"],
                description=call["description"],
                deadline=call.get("deadline"),
                funding_amount=call.get("funding_amount"),
                eligibility=call.get("eligibility"),
                url=call["url"],
                source="grants_gov",
            ))

    # Try live Grants.gov search via public search page
    grants += await _scrape_grants_gov(profile, max_results - len(grants))
    _info(f"[grants.gov] found {len(grants)} opportunities")
    return grants[:max_results]


async def _scrape_grants_gov(profile: ClientProfileInput, max_results: int) -> list[GrantInput]:
    """Scrape Grants.gov public search for additional live results."""
    if max_results <= 0:
        return []
    keywords = "+".join(profile.keywords[:3]) if profile.keywords else "+".join(profile.research_areas[:2])
    url = f"https://grants.gov/search-results.html?keywords={keywords}&status=POSTED&sortBy=openDate%7Cdesc"
    grants = []
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
            soup = BeautifulSoup(resp.text, "lxml")
        for row in soup.select("tr[class*=grant], .grant-row, [data-opportunity-number]")[:max_results]:
            title_el = row.find(["h3", "h4", "a", "td"])
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if len(title) < 10:
                continue
            link = title_el.find("a")
            href = link.get("href", "") if link else ""
            full_url = ("https://grants.gov" + href) if href.startswith("/") else href or "https://grants.gov/search-results.html"
            grants.append(GrantInput(
                id=f"ggov-live-{abs(hash(title)) % 999999}",
                title=title[:200],
                agency="US Federal (Grants.gov)",
                description=row.get_text(strip=True)[:400],
                deadline=None,
                funding_amount=None,
                eligibility=None,
                url=full_url,
                source="grants_gov",
            ))
    except Exception as exc:
        _warn(f"[grants.gov] scrape error: {exc}")
    return grants[:max_results]
