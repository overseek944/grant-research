"""
India grant sources: BIRAC, DST, DBT, ICMR, Atal Innovation Mission.
All are scraped (no public APIs available). Falls back gracefully on failure.
"""
import httpx
from bs4 import BeautifulSoup
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

SOURCES = [
    {
        "name": "BIRAC",
        "agency": "BIRAC (Biotechnology Industry Research Assistance Council)",
        "url": "https://birac.nic.in/cfp.php",
        "source_id": "birac",
    },
    {
        "name": "DST",
        "agency": "DST (Department of Science & Technology, India)",
        "url": "https://dst.gov.in/call-for-proposals",
        "source_id": "dst",
    },
    {
        "name": "DBT",
        "agency": "DBT (Department of Biotechnology, India)",
        "url": "https://dbtindia.gov.in/schemes-programmes/research-development",
        "source_id": "dbt",
    },
    {
        "name": "ICMR",
        "agency": "ICMR (Indian Council of Medical Research)",
        "url": "https://main.icmr.nic.in/content/call-proposals",
        "source_id": "icmr",
    },
]

# Known standing programmes (used when live scraping fails or returns nothing)
STANDING_PROGRAMMES: list[dict] = [
    {
        "id": "birac-bipp",
        "title": "BIRAC Biotechnology Ignition Grant (BIG) Scheme",
        "agency": "BIRAC",
        "description": "Early-stage biotech startups and innovators. Funding up to ₹50 lakhs for proof-of-concept, prototype development, and technology validation. Open on a rolling basis.",
        "funding_amount": "Up to ₹50,00,000",
        "eligibility": "Indian startups, MSMEs, individual innovators with biotech focus",
        "url": "https://birac.nic.in/BIG.php",
        "source": "birac",
    },
    {
        "id": "dst-inspire",
        "title": "DST INSPIRE Faculty Award",
        "agency": "DST – INSPIRE Programme",
        "description": "Contractual research positions for young researchers (age ≤ 32) to build independent research careers. ₹3.5 lakh/month + ₹7 lakh research grant annually for 5 years.",
        "funding_amount": "₹3.5 lakh/month + ₹7 lakh/year research grant",
        "eligibility": "Indian nationals aged ≤32 with PhD; returning from abroad encouraged",
        "url": "https://www.inspire-dst.gov.in/faculty.html",
        "source": "dst",
    },
    {
        "id": "dst-serb-core",
        "title": "SERB CORE Research Grant",
        "agency": "SERB (Science and Engineering Research Board)",
        "description": "Core Research Grants for basic research in frontier areas of science & engineering. ₹15–60 lakhs over 3 years. Regular cycles throughout the year.",
        "funding_amount": "₹15–60 lakhs over 3 years",
        "eligibility": "Faculty/scientists at Indian universities and national laboratories",
        "url": "https://serb.gov.in/page/english/schemes_CRG.php",
        "source": "dst",
    },
    {
        "id": "dbt-ner",
        "title": "DBT Twinning Programme for NER Institutions",
        "agency": "DBT – North East Region Initiative",
        "description": "Collaborative projects between North-Eastern institutions and premier national institutions. ₹30–75 lakhs for 3-year projects in biotech, health, and agriculture.",
        "funding_amount": "₹30–75 lakhs for 3 years",
        "eligibility": "North-Eastern universities and colleges in partnership with premier institutions",
        "url": "https://dbtindia.gov.in/schemes-programmes/research-development/twinning-programme",
        "source": "dbt",
    },
    {
        "id": "icmr-extramural",
        "title": "ICMR Extramural Research Programme",
        "agency": "ICMR",
        "description": "Biomedical research grants for addressing national health priorities. Short-term (3 years, ₹30–80 lakhs) and long-term (5 years, up to ₹2 crores). Rolling calls.",
        "funding_amount": "₹30 lakhs – ₹2 crore",
        "eligibility": "Indian medical colleges, research institutions, and public health organisations",
        "url": "https://main.icmr.nic.in/content/research-scheme-extramural",
        "source": "icmr",
    },
    {
        "id": "aim-atal",
        "title": "Atal New India Challenge (ANIC)",
        "agency": "Atal Innovation Mission, NITI Aayog",
        "description": "Challenges for innovators and startups to solve national sectoral problems. Grants of ₹1–2 crores for product/technology development in health, water, agriculture, education.",
        "funding_amount": "₹1–2 crore",
        "eligibility": "Indian startups, SMEs, and innovators; prototypes or near-market solutions",
        "url": "https://aim.gov.in/atal-new-india-challenges.php",
        "source": "aim",
    },
    {
        "id": "csir-nih",
        "title": "CSIR Network Projects (NIH-CSIR Bilateral Programme)",
        "agency": "CSIR (Council of Scientific and Industrial Research)",
        "description": "Collaborative research between Indian CSIR labs and NIH/US institutions. Focus areas: infectious disease, genomics, computational biology.",
        "funding_amount": "Varies by project ($100,000–$500,000 US component)",
        "eligibility": "CSIR laboratories; US NIH collaboration required",
        "url": "https://www.csir.res.in/funding-opportunities",
        "source": "csir",
    },
    {
        "id": "gates-india",
        "title": "Gates Foundation India Programs (Grand Challenges India)",
        "agency": "Bill & Melinda Gates Foundation",
        "description": "Grand Challenges India funds bold ideas in global health, nutrition, and sanitation with direct India focus. Phase 1: $100,000 exploration. Phase 2: up to $1M for proof of concept.",
        "funding_amount": "$100,000 (Phase 1) → up to $1,000,000 (Phase 2)",
        "eligibility": "Indian researchers, startups, and NGOs; all sectors welcome",
        "url": "https://gcgh.grandchallenges.org/",
        "source": "foundations",
    },
    {
        "id": "wellcome-india",
        "title": "Wellcome Trust India Alliance – Intermediate & Senior Fellowships",
        "agency": "Wellcome Trust – DBT India Alliance",
        "description": "Five-year research fellowships for biomedical and health researchers in India. Intermediate: up to £400,000. Senior: up to £800,000. Rolling calls throughout the year.",
        "funding_amount": "£400,000–£800,000 over 5 years",
        "eligibility": "Researchers at Indian institutions at postdoctoral or faculty level",
        "url": "https://www.indiaalliance.org/fellowships",
        "source": "foundations",
    },
]


@activity.defn
async def search_india_grants(profile: ClientProfileInput, max_results: int = 25) -> list[GrantInput]:
    grants: list[GrantInput] = []
    kw_lower = [k.lower() for k in profile.keywords + profile.research_areas]

    # Try live scraping
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for source in SOURCES:
            try:
                resp = await client.get(source["url"], headers={"User-Agent": "Mozilla/5.0"})
                soup = BeautifulSoup(resp.text, "lxml")
                links = soup.find_all("a", href=True)
                for a in links[:60]:
                    text = a.get_text(strip=True)
                    if len(text) < 20 or len(text) > 300:
                        continue
                    tl = text.lower()
                    if any(k in tl for k in ["grant", "scheme", "call", "proposal", "fellowship", "award", "fund"]):
                        href = a["href"]
                        full_url = href if href.startswith("http") else source["url"].rstrip("/") + "/" + href.lstrip("/")
                        grants.append(GrantInput(
                            id=f"{source['source_id']}-{abs(hash(text)) % 999999}",
                            title=text[:200],
                            agency=source["agency"],
                            description=text,
                            deadline=None,
                            funding_amount=None,
                            eligibility="Indian institutions",
                            url=full_url,
                            source=source["source_id"],
                        ))
            except Exception as exc:
                _warn(f"[india_grants] scrape failed for {source['name']}: {exc}")

    # Always include standing programmes (filtered by relevance to profile)
    for prog in STANDING_PROGRAMMES:
        desc_lower = prog["description"].lower() + " " + prog["title"].lower()
        is_relevant = (
            not kw_lower  # include all if no keywords
            or any(k in desc_lower for k in kw_lower)
            or any(k in prog["agency"].lower() for k in kw_lower)
        )
        if is_relevant:
            grants.append(GrantInput(
                id=prog["id"],
                title=prog["title"],
                agency=prog["agency"],
                description=prog["description"],
                deadline="Rolling / check website",
                funding_amount=prog.get("funding_amount"),
                eligibility=prog.get("eligibility"),
                url=prog["url"],
                source=prog["source"],
            ))

    # Deduplicate by title
    seen: set[str] = set()
    unique: list[GrantInput] = []
    for g in grants:
        key = g.title[:60].lower()
        if key not in seen:
            seen.add(key)
            unique.append(g)

    _info(f"[india_grants] found {len(unique)} opportunities")
    return unique[:max_results]
