"""
NSF Awards API – National Science Foundation funded programs and active solicitations.
No API key required.  Docs: https://www.research.gov/common/webapi/awardapisearch-v1.htm
"""
import httpx
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

NSF_AWARDS_URL = "https://api.nsf.gov/services/v1/awards.json"
NSF_PROGRAMS_URL = "https://www.nsf.gov/funding/pgm_list.jsp"  # HTML, scraped separately


@activity.defn
async def search_nsf(profile: ClientProfileInput, max_results: int = 20) -> list[GrantInput]:
    grants: list[GrantInput] = []
    keyword = " ".join(profile.keywords[:2] + profile.research_areas[:1]) if profile.keywords else " ".join(profile.research_areas[:2])
    _info(f"[nsf] searching: {keyword}")
    params = {
        "keyword": keyword,
        "dateStart": "01/01/2024",
        "printFields": "id,title,agency,awardeeName,piFirstName,piLastName,abstractText,startDate,expDate,fundsObligatedAmt,awardsURL,primaryProgram",
        "rpp": min(max_results, 25),
        "offset": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(NSF_AWARDS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        awards = data.get("response", {}).get("award", [])
        for a in awards:
            award_id = a.get("id", "")
            title = a.get("title", "Untitled NSF Award")
            abstract = a.get("abstractText", "")[:500]
            amount = a.get("fundsObligatedAmt", "")
            exp_date = a.get("expDate", "")
            pi = f"{a.get('piFirstName', '')} {a.get('piLastName', '')}".strip()
            program = a.get("primaryProgram", "NSF")
            url = a.get("awardsURL") or f"https://www.nsf.gov/awardsearch/showAward?AWD_ID={award_id}"
            grants.append(GrantInput(
                id=f"nsf-{award_id}",
                title=title,
                agency=f"NSF – {program}" if program else "NSF",
                description=abstract or title,
                deadline=exp_date or None,
                funding_amount=f"${int(float(amount)):,}" if amount else None,
                eligibility="US institutions; international collaborators eligible",
                url=url,
                source="nsf",
            ))
        _info(f"[nsf] found {len(grants)} awards")
    except Exception as exc:
        _warn(f"[nsf] error: {exc}")
    return grants
