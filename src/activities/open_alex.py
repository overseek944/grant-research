"""
OpenAlex — 250M research works, completely free, no API key.
Builds institution research profile: top concepts, past funders, key researchers.
API: https://api.openalex.org
"""
import httpx
import logging
from temporalio import activity
from ..models import ClientProfileInput

_log = logging.getLogger("grant-agent")

def _info(msg):
    try: activity.logger.info(msg)
    except Exception: _log.info(msg)

def _warn(msg):
    try: activity.logger.warning(msg)
    except Exception: _log.warning(msg)

_HEADERS = {"User-Agent": "grant-agent/1.0 (research intelligence service)"}


@activity.defn
async def get_institution_profile(institution_name: str) -> dict:
    """
    Returns a rich research profile built from OpenAlex data:
    {
      ror, display_name, works_count, cited_by_count,
      concepts: [top 8 research domains],
      past_funders: [funders found in publication acknowledgements],
      top_researchers: [{name, concept}],
    }
    """
    base = "https://api.openalex.org"

    # Build a list of name variants to try (from specific → broad)
    name_variants = [institution_name]
    for sep in [" – ", " — ", " - ", ", Department", " Department"]:
        if sep in institution_name:
            name_variants.append(institution_name.split(sep)[0].strip())
    # Also try dropping leading "The "
    cleaned = institution_name.replace("The ", "").strip()
    if cleaned not in name_variants:
        name_variants.append(cleaned)

    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        # Step 1: resolve institution → try name variants until one matches
        results = []
        matched_name = institution_name
        for variant in name_variants:
            resp = await client.get(
                f"{base}/institutions",
                params={"search": variant, "per-page": 1},
                headers=_HEADERS,
            )
            results = resp.json().get("results", [])
            if results:
                matched_name = variant
                break

        if not results:
            _warn(f"[openalex] institution not found: {institution_name}")
            return {"display_name": institution_name, "concepts": [], "past_funders": [], "top_researchers": []}

        inst = results[0]
        openalex_id = inst.get("id", "")  # e.g. "https://openalex.org/I166441858"
        ror_full = inst.get("ror", "")
        ror = ror_full.replace("https://ror.org/", "")
        display_name = inst.get("display_name", institution_name)
        works_count = inst.get("works_count", 0)
        cited_by_count = inst.get("cited_by_count", 0)

        # Extract institution-level research topics (new field name in OpenAlex)
        topics = inst.get("topics", [])
        top_concepts = [t["display_name"] for t in sorted(topics, key=lambda x: -x.get("count", 0))[:8]]

        _info(f"[openalex] {display_name} — {works_count:,} works, {cited_by_count:,} citations")

        if not openalex_id:
            return {
                "ror": ror, "display_name": display_name, "concepts": top_concepts,
                "past_funders": [], "top_researchers": [],
                "works_count": works_count, "cited_by_count": cited_by_count,
            }

        # Step 2: pull top cited works — use short ID (not full URL) for filter
        short_id = openalex_id.replace("https://openalex.org/", "")
        works_resp = await client.get(
            f"{base}/works",
            params={
                "filter": f"institutions.id:{short_id}",
                "sort": "cited_by_count:desc",
                "per-page": 40,
                "select": "title,funders,authorships,topics",
            },
            headers=_HEADERS,
        )
        works = works_resp.json().get("results", [])

        funder_counts: dict[str, int] = {}
        researcher_map: dict[str, str] = {}

        for work in works:
            # Funders from the `funders` field (replaces old `grants` field)
            for f in work.get("funders", []):
                fname = f.get("display_name", "").strip()
                if fname and len(fname) > 3:
                    funder_counts[fname] = funder_counts.get(fname, 0) + 1

            # Top author per work → their primary topic
            for auth in work.get("authorships", [])[:1]:
                aname = auth.get("author", {}).get("display_name", "")
                if aname and aname not in researcher_map:
                    top_t = work.get("topics", [])
                    researcher_map[aname] = top_t[0]["display_name"] if top_t else ""

        past_funders = [f for f, _ in sorted(funder_counts.items(), key=lambda x: -x[1])][:10]
        top_researchers = [{"name": n, "concept": c} for n, c in list(researcher_map.items())[:6]]

        _info(f"[openalex] found {len(past_funders)} past funders, {len(top_researchers)} researchers")

        return {
            "ror": ror,
            "display_name": display_name,
            "works_count": works_count,
            "cited_by_count": cited_by_count,
            "concepts": top_concepts,
            "past_funders": past_funders,
            "top_researchers": top_researchers,
        }
