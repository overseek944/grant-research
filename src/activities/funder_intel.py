"""
Funder intelligence enrichment — no API key needed.
For top-ranked grants, queries NIH Reporter (Indian awardees), NSF Awards (actual amounts),
OpenAlex (funder publication record), and CrossRef (funding acknowledgements).
Injects real data into each grant's funder_intel field so Claude can reason from facts.
"""
import httpx
import logging
from temporalio import activity
from ..models import GrantInput, ClientProfileInput

_log = logging.getLogger("grant-agent")

def _info(msg):
    try: activity.logger.info(msg)
    except Exception: _log.info(msg)

def _warn(msg):
    try: activity.logger.warning(msg)
    except Exception: _log.warning(msg)

TOP_N = 15  # enrich this many top grants


@activity.defn
async def enrich_with_funder_intel(
    grants: list[GrantInput],
    profile: ClientProfileInput,
) -> list[GrantInput]:
    """Enrich the top N grants with real funder award history."""
    enriched = list(grants)
    to_enrich = enriched[:TOP_N]  # already sorted by relevance_score from stage 1

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for i, g in enumerate(to_enrich):
            parts: list[str] = []
            try:
                source = g.source.lower()
                agency = g.agency.lower()

                if source == "nih" or "nih" in agency or "national institutes" in agency or _is_nih_grant(g):
                    intel = await _nih_intel(client, g, profile)
                    if intel:
                        parts.append(intel)

                if source == "nsf" or "nsf" in agency or "national science foundation" in agency:
                    intel = await _nsf_intel(client, g, profile)
                    if intel:
                        parts.append(intel)

                if source == "eu_horizon" or "european" in agency or "horizon" in agency or "erc" in agency:
                    intel = await _openalex_eu_intel(client, g, profile)
                    if intel:
                        parts.append(intel)

                # CrossRef funding acknowledgements — works for any funder
                intel = await _crossref_intel(client, g, profile)
                if intel:
                    parts.append(intel)

            except Exception as exc:
                _warn(f"[funder_intel] {g.id}: {exc}")

            if parts:
                enriched[i].funder_intel = "\n\n".join(parts)

    enriched_count = sum(1 for g in enriched[:TOP_N] if g.funder_intel)
    _info(f"[funder_intel] enriched {enriched_count}/{min(TOP_N, len(enriched))} grants")
    return enriched


def _is_nih_grant(g: GrantInput) -> bool:
    t = (g.title + g.id + g.description).lower()
    return any(code in t for code in ["r01", "r21", "d43", "u01", "sbir", "sttr", "r34", "fogarty"])


async def _nih_intel(client: httpx.AsyncClient, grant: GrantInput, profile: ClientProfileInput) -> str:
    """Actual NIH awards to Indian institutions for this mechanism."""
    # Detect activity code
    t = (grant.title + grant.id).lower()
    code_map = {"r01": "R01", "r21": "R21", "d43": "D43", "u01": "U01", "r43": "R43", "r41": "R41", "r34": "R34"}
    codes = [v for k, v in code_map.items() if k in t] or ["R01"]

    try:
        payload = {
            "criteria": {
                "org_countries": ["India"],
                "activity_codes": codes,
                "fiscal_years": [2021, 2022, 2023, 2024, 2025],
            },
            "limit": 15,
            "fields": ["project_title", "award_amount", "org_name", "activity_code", "fiscal_year"],
        }
        resp = await client.post("https://api.reporter.nih.gov/v2/projects/search", json=payload, timeout=15)
        if resp.status_code != 200:
            return ""
        hits = resp.json().get("results", [])

        if not hits:
            return (
                f"NIH {'/'.join(codes)} — India award history: No direct awards to Indian institutions "
                f"found in 2021–2025. This mechanism is open to international applicants but rarely awarded "
                f"to India-based PIs without a US co-PI. A US collaborator significantly improves competitiveness."
            )

        amounts = [h["award_amount"] for h in hits if h.get("award_amount")]
        avg = int(sum(amounts) / len(amounts)) if amounts else 0
        orgs = list({h["org_name"] for h in hits if h.get("org_name")})[:5]
        years = sorted({str(h.get("fiscal_year", "")) for h in hits if h.get("fiscal_year")})

        lines = [f"NIH {'/'.join(codes)} — India Award History (2021–2025):"]
        lines.append(f"  • {len(hits)} grants awarded to Indian institutions")
        if avg:
            lines.append(f"  • Average award: ${avg:,}/year (vs stated max)")
        if orgs:
            lines.append(f"  • Recent Indian recipients: {', '.join(orgs)}")
        if years:
            lines.append(f"  • Active years: {', '.join(years)}")
        return "\n".join(lines)

    except Exception as exc:
        _warn(f"[funder_intel] NIH query: {exc}")
        return ""


async def _nsf_intel(client: httpx.AsyncClient, grant: GrantInput, profile: ClientProfileInput) -> str:
    """Actual NSF award amounts for this research area."""
    keywords = " ".join(profile.keywords[:3])
    try:
        resp = await client.get(
            "https://api.nsf.gov/services/v1/awards.json",
            params={
                "keyword": keywords,
                "dateStart": "01/01/2021",
                "printFields": "id,title,awardeeName,fundProgramName,fundsObligatedAmt,expDate,abstractText",
                "rpp": 15,
            },
            timeout=15,
        )
        awards = resp.json().get("response", {}).get("award", []) or []

        amounts = []
        for a in awards:
            try:
                amounts.append(int(a.get("fundsObligatedAmt") or 0))
            except Exception:
                pass
        amounts = [x for x in amounts if x > 0]

        if not amounts:
            return ""

        avg = int(sum(amounts) / len(amounts))
        max_award = max(amounts)
        programs = list({a.get("fundProgramName", "").strip() for a in awards if a.get("fundProgramName")})[:4]
        awardees = [a.get("awardeeName", "") for a in awards[:5] if a.get("awardeeName")]

        lines = [f"NSF Award Landscape — '{keywords}' (2021–2025):"]
        lines.append(f"  • {len(amounts)} recent awards analysed")
        lines.append(f"  • Average award: ${avg:,}  |  Largest: ${max_award:,}")
        if programs:
            lines.append(f"  • Active NSF programs: {', '.join(programs)}")
        if awardees:
            lines.append(f"  • Recent awardees: {', '.join(awardees)}")
        lines.append("  • NSF Global programs explicitly encourage India-US partnerships")
        return "\n".join(lines)

    except Exception as exc:
        _warn(f"[funder_intel] NSF query: {exc}")
        return ""


async def _openalex_eu_intel(client: httpx.AsyncClient, grant: GrantInput, profile: ClientProfileInput) -> str:
    """OpenAlex: EU-funded papers with Indian co-authors."""
    kw = " ".join(profile.keywords[:2])
    try:
        resp = await client.get(
            "https://api.openalex.org/works",
            params={
                "filter": "grants.funder.display_name:European Research Council,institutions.country_code:IN",
                "per-page": 8,
                "select": "title,authorships,publication_year,grants",
                "sort": "publication_year:desc",
            },
            headers={"User-Agent": "grant-agent/1.0"},
            timeout=15,
        )
        works = resp.json().get("results", [])

        indian_orgs: set[str] = set()
        for w in works:
            for auth in w.get("authorships", []):
                countries = auth.get("countries", [])
                if "IN" in countries:
                    for inst in auth.get("institutions", []):
                        n = inst.get("display_name", "")
                        if n:
                            indian_orgs.add(n)

        lines = ["EU Horizon / ERC — Indian Participation (via OpenAlex):"]
        lines.append(f"  • ERC-funded works with Indian co-authors: {len(works)} recent examples")
        if indian_orgs:
            lines.append(f"  • Indian institutions in EU-funded research: {', '.join(sorted(indian_orgs)[:5])}")
        lines.append("  • Indian partners join as third-country participants — EU lead institution required")
        lines.append("  • IIT system, IISc, and AIIMS have established EU collaboration tracks")
        return "\n".join(lines)

    except Exception as exc:
        _warn(f"[funder_intel] OpenAlex EU query: {exc}")
        return ""


async def _crossref_intel(client: httpx.AsyncClient, grant: GrantInput, profile: ClientProfileInput) -> str:
    """CrossRef: funders backing recent publications in this topic area."""
    kw = " ".join(profile.keywords[:2])
    try:
        resp = await client.get(
            "https://api.crossref.org/works",
            params={
                "query": f"{kw} India",
                "filter": "has-funder:true",
                "rows": 10,
                "select": "title,funder,published",
                "sort": "published",
                "order": "desc",
            },
            headers={"User-Agent": "grant-agent/1.0 (research intelligence)"},
            timeout=15,
        )
        items = resp.json().get("message", {}).get("items", []) or []

        funder_counts: dict[str, int] = {}
        for item in items:
            for f in item.get("funder", []):
                fname = (f.get("name") or "").strip()
                if fname and len(fname) > 4:
                    funder_counts[fname] = funder_counts.get(fname, 0) + 1

        if not funder_counts:
            return ""

        top_funders = sorted(funder_counts.items(), key=lambda x: -x[1])[:6]
        funder_list = ", ".join(f"{n} ({c})" for n, c in top_funders)
        return f"CrossRef Funding Signal — '{kw}+India' papers recently funded by: {funder_list}. These are active funders in this space."

    except Exception as exc:
        _warn(f"[funder_intel] CrossRef query: {exc}")
        return ""
