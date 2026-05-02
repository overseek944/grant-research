"""
NIH Reporter API – recent NIH-funded projects (shows what NIH is funding NOW).
No API key required.  Docs: https://api.reporter.nih.gov/
Also scrapes NIH Guide RSS for active funding opportunities.
"""
import httpx
import xml.etree.ElementTree as ET
from temporalio import activity
import logging
_log = logging.getLogger("grant-agent")

def _info(msg):
    try: activity.logger.info(msg)
    except Exception: _log.info(msg)

def _warn(msg):
    try: activity.logger.warning(msg)
    except Exception: _log.warning(msg)

from ..models import GrantInput, ClientProfileInput

REPORTER_URL = "https://api.reporter.nih.gov/v2/projects/search"
NIH_GUIDE_RSS = "https://grants.nih.gov/rss/new_opportunities.cfm?Version=2"  # active FOAs


@activity.defn
async def search_nih(profile: ClientProfileInput, max_results: int = 20) -> list[GrantInput]:
    grants: list[GrantInput] = []

    # --- 1. NIH Reporter: recent funded projects (shows funding priority signals) ---
    terms = profile.keywords[:6] + profile.research_areas[:3]
    query_text = " AND ".join(terms[:4]) if terms else "public health"
    body = {
        "criteria": {
            "advanced_text_search": {
                "operator": "and",
                "search_field": "all",
                "search_text": query_text,
            }
        },
        "limit": min(max_results, 25),
        "offset": 0,
        "sort_field": "fiscal_year",
        "sort_order": "desc",
    }
    _info(f"[nih_reporter] querying: {query_text}")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(REPORTER_URL, json=body)
            resp.raise_for_status()
            data = resp.json()
        for p in data.get("results", []):
            appl_id = str(p.get("appl_id", ""))
            pi_names = ", ".join([n.get("full_name", "") for n in p.get("principal_investigators", [])])
            grants.append(GrantInput(
                id=f"nih-{appl_id}",
                title=p.get("project_title", "Untitled NIH Project"),
                agency="NIH – " + p.get("agency_ic_fundings", [{}])[0].get("abbreviated_name", "Unknown IC"),
                description=p.get("abstract_text", "")[:500] or p.get("project_title", ""),
                deadline=str(p.get("fiscal_year", "")),
                funding_amount=f"${p.get('award_amount', 0):,.0f}" if p.get("award_amount") else None,
                eligibility="US institutions and international collaborators",
                url=f"https://reporter.nih.gov/project-details/{appl_id}" if appl_id else "https://reporter.nih.gov",
                source="nih",
            ))
    except Exception as exc:
        _warn(f"[nih_reporter] projects error: {exc}")



    _info(f"[nih] found {len(grants)} total")
    return grants[:max_results]
