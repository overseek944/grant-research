"""
EU Funding & Tenders Portal – Horizon Europe opportunities.
Uses public search page scraping + curated active calls as fallback.
"""
import logging
import httpx
from bs4 import BeautifulSoup
from temporalio import activity
_log = logging.getLogger("grant-agent")

def _info(msg):
    try: activity.logger.info(msg)
    except Exception: _log.info(msg)

def _warn(msg):
    try: activity.logger.warning(msg)
    except Exception: _log.warning(msg)

from ..models import GrantInput, ClientProfileInput

# Curated active Horizon Europe programmes (updated May 2025)
ACTIVE_HORIZON_CALLS = [
    {
        "id": "horiz-erc-stg-2025",
        "title": "ERC Starting Grant 2025",
        "agency": "EU – European Research Council",
        "description": "Supports excellent researchers of any nationality with 2–7 years post-PhD experience hosting their project in an EU member state. €1.5M over 5 years. No topic restriction — scientific excellence is the sole criterion.",
        "funding_amount": "Up to €1,500,000",
        "deadline": "2025-08-20",
        "eligibility": "PhD holders with 2–7 years post-doctoral experience; hosted at EU or associated country institution",
        "url": "https://erc.europa.eu/funding/starting-grants",
    },
    {
        "id": "horiz-msca-pf-2025",
        "title": "MSCA Postdoctoral Fellowships 2025",
        "agency": "EU – Marie Skłodowska-Curie Actions",
        "description": "Two-year fellowships for experienced researchers to carry out research in Europe or worldwide. Supports researcher mobility, interdisciplinary and intersectoral collaboration.",
        "funding_amount": "~€180,000 over 2 years",
        "deadline": "2025-09-10",
        "eligibility": "Doctoral degree holders; must move to a different country than last residence",
        "url": "https://marie-sklodowska-curie-actions.ec.europa.eu/actions/postdoctoral-fellowships",
    },
    {
        "id": "horiz-health-2025",
        "title": "Horizon Europe Health Cluster – Open Calls 2025",
        "agency": "EU – Horizon Europe Cluster 1 (Health)",
        "description": "Open calls for proposals in infectious diseases, cancer, non-communicable diseases, digital health, and global health. Collaborative consortia required (3+ entities from 3+ EU member states).",
        "funding_amount": "€4M–€12M per project",
        "deadline": "2025-10-15",
        "eligibility": "Consortia of 3+ entities from 3+ EU/associated countries; Indian partners eligible as third-country participants",
        "url": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-search;callCode=HORIZON-HLTH-2025",
    },
    {
        "id": "horiz-global-health-2025",
        "title": "Horizon Europe Global Health & International Partnerships",
        "agency": "EU – Horizon Europe International",
        "description": "Research partnerships between EU and low-and-middle-income countries. Focuses on infectious diseases, malnutrition, maternal health, and health systems. Indian institutions regularly lead or participate.",
        "funding_amount": "€5M–€15M per consortium",
        "deadline": "2025-11-30",
        "eligibility": "Consortia including LMIC partners; Indian universities and research institutes eligible",
        "url": "https://research-and-innovation.ec.europa.eu/funding/funding-opportunities/funding-programmes-and-open-calls/horizon-europe/global-health_en",
    },
    {
        "id": "horiz-eic-accelerator-2025",
        "title": "EIC Accelerator – Deep Tech Innovation",
        "agency": "EU – European Innovation Council",
        "description": "Grant + equity investment for deep tech startups and SMEs developing breakthrough technologies. Medical devices, diagnostics, health tech are priority areas. €500K grant + up to €15M equity.",
        "funding_amount": "Up to €500,000 grant + €15,000,000 equity",
        "deadline": "2025-10-08",
        "eligibility": "SMEs established in EU or associated country; Indian subsidiaries of EU companies eligible",
        "url": "https://eic.ec.europa.eu/eic-funding-opportunities/eic-accelerator_en",
    },
]


@activity.defn
async def search_eu_horizon(profile: ClientProfileInput, max_results: int = 20) -> list[GrantInput]:
    grants: list[GrantInput] = []
    kw_lower = [k.lower() for k in profile.keywords + profile.research_areas]

    # Filter active calls by relevance to client profile
    for call in ACTIVE_HORIZON_CALLS:
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
                source="eu_horizon",
            ))

    # Also scrape EU portal for current open calls
    grants += await _scrape_eu_search(profile, max_results - len(grants))
    _info(f"[eu_horizon] found {len(grants)} results")
    return grants[:max_results]


async def _scrape_eu_search(profile: ClientProfileInput, max_results: int) -> list[GrantInput]:
    """Scrape EU Funding Portal open calls with keyword search."""
    if max_results <= 0:
        return []
    kw = " ".join(profile.keywords[:2])
    url = f"https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-search;keywords={kw}"
    grants = []
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            soup = BeautifulSoup(resp.text, "lxml")
        for el in soup.select("[class*=topic-item], h3, h4")[:max_results * 2]:
            text = el.get_text(strip=True)
            if len(text) < 20 or len(text) > 300:
                continue
            parent_link = el.find_parent("a") or el.find("a")
            href = parent_link.get("href", "") if parent_link else ""
            full_url = ("https://ec.europa.eu" + href) if href.startswith("/") else href or url
            grants.append(GrantInput(
                id=f"eu-scrape-{abs(hash(text)) % 999999}",
                title=text[:150],
                agency="EU – Horizon Europe",
                description=text,
                deadline=None,
                funding_amount=None,
                eligibility="EU and associated countries; global partnerships available",
                url=full_url,
                source="eu_horizon",
            ))
    except Exception as exc:
        _warn(f"[eu_horizon] scrape error: {exc}")
    return grants[:max_results]
