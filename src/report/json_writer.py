"""
Serializes a completed grant research run to structured JSON.
"""
from datetime import datetime, timezone
from ..models import GrantInput, ClientProfile


def write_run_json(
    grants: list[GrantInput],
    profile: ClientProfile,
    sources_searched: list[str],
    institution_profile: dict,
) -> dict:
    """Return the full run as a JSON-serialisable dict."""
    return {
        "client": {
            "name": profile.name,
            "institution_type": profile.institution_type,
            "research_areas": profile.research_areas,
            "focus_geographies": profile.focus_geographies,
            "keywords": profile.keywords,
        },
        "run": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sources_searched": sources_searched,
            "total_found": len(grants),
            "high_priority": sum(1 for g in grants if g.relevance_score >= 0.65),
            "worth_reviewing": sum(1 for g in grants if 0.35 <= g.relevance_score < 0.65),
        },
        "institution_profile": {
            "display_name": institution_profile.get("display_name", profile.name),
            "works_count": institution_profile.get("works_count"),
            "cited_by_count": institution_profile.get("cited_by_count"),
            "concepts": institution_profile.get("concepts", []),
            "past_funders": institution_profile.get("past_funders", []),
            "top_researchers": institution_profile.get("top_researchers", []),
        },
        "grants": [
            {
                "id": g.id,
                "title": g.title,
                "agency": g.agency,
                "description": g.description,
                "deadline": g.deadline,
                "funding_amount": g.funding_amount,
                "eligibility": g.eligibility,
                "url": g.url,
                "source": g.source,
                "relevance_score": round(g.relevance_score, 3),
                "category": g.category,
                "days_until_deadline": g.days_until_deadline,
                "brief": g.brief,
                "funder_intel": g.funder_intel,
                "strategy_memo": g.strategy_memo,
            }
            for g in sorted(grants, key=lambda x: -x.relevance_score)
        ],
    }
