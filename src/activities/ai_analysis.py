"""
Three-stage Claude analysis pipeline — no API key, uses claude CLI.

Stage 1 — Fast Screen   : all grants, batch 20, score + category only (~30s)
Stage 2 — Deep Brief    : top 15, batch 5, 280-word expert brief + refined score (~4 min)
Stage 3 — Strategy Memo : top 5, one per call, 380-word "how to win" memo (~5 min)
"""
import asyncio
import json
import math
import subprocess
import re
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

SCREEN_BATCH = 20
DEEP_BATCH = 5
TOP_N_DEEP = 15
TOP_N_STRATEGY = 5


@activity.defn
async def analyze_grants_with_claude(
    grants: list[GrantInput],
    profile: ClientProfileInput,
    institution_profile: dict = None,
) -> list[GrantInput]:
    if not grants:
        return []

    inst = institution_profile or {}
    _info(f"[claude] 3-stage analysis — {len(grants)} grants for {profile.name}")

    profile_text = _build_profile_text(profile, inst)
    enriched = list(grants)

    # ── STAGE 1: Fast Screen ──────────────────────────────────────────────
    _info(f"[claude] stage 1 — screening {len(grants)} grants")
    screen_input = [{"index": i, "title": g.title, "agency": g.agency, "desc": g.description[:120]}
                    for i, g in enumerate(grants)]

    screen_results: list[dict] = []
    num_batches = math.ceil(len(screen_input) / SCREEN_BATCH)
    for bn, bs in enumerate(range(0, len(screen_input), SCREEN_BATCH), 1):
        batch = screen_input[bs : bs + SCREEN_BATCH]
        indexed = [{**g, "index": i} for i, g in enumerate(batch)]
        _info(f"[claude] screen {bn}/{num_batches} ({len(indexed)} grants)")
        raw = await asyncio.get_event_loop().run_in_executor(None, _run_claude, _screen_prompt(indexed, profile_text))
        parsed = _safe_parse(raw)
        for item in parsed:
            if "index" in item:
                item["index"] += bs
        screen_results.extend(parsed)

    for item in screen_results:
        idx = item.get("index")
        if idx is not None and 0 <= idx < len(enriched):
            enriched[idx].relevance_score = float(item.get("relevance_score", 0.0))
            enriched[idx].category = item.get("category", "other")

    _info(f"[claude] stage 1 done — {len(screen_results)} screened")

    # ── Funder Intelligence (runs between stage 1 and 2) ─────────────────
    try:
        from .funder_intel import enrich_with_funder_intel
        _info(f"[claude] enriching top {TOP_N_DEEP} grants with funder award data")
        enriched = await enrich_with_funder_intel(enriched, profile)
    except Exception as exc:
        _warn(f"[claude] funder intel skipped: {exc}")

    # ── STAGE 2: Deep Brief ───────────────────────────────────────────────
    top_indices = sorted(range(len(enriched)), key=lambda i: -enriched[i].relevance_score)[:TOP_N_DEEP]
    _info(f"[claude] stage 2 — deep brief for {len(top_indices)} grants")

    deep_input = []
    for rank, orig_idx in enumerate(top_indices):
        g = enriched[orig_idx]
        deep_input.append({
            "rank": rank,
            "orig_index": orig_idx,
            "title": g.title,
            "agency": g.agency,
            "description": g.description[:450],
            "funding": g.funding_amount or "unspecified",
            "deadline": g.deadline or "unspecified",
            "eligibility": g.eligibility or "unspecified",
            "funder_intel": g.funder_intel or "not available",
        })

    deep_results: list[dict] = []
    num_deep_batches = math.ceil(len(deep_input) / DEEP_BATCH)
    for bn, bs in enumerate(range(0, len(deep_input), DEEP_BATCH), 1):
        batch = deep_input[bs : bs + DEEP_BATCH]
        indexed = [{**g, "rank": i} for i, g in enumerate(batch)]
        _info(f"[claude] deep {bn}/{num_deep_batches} ({len(indexed)} grants)")
        raw = await asyncio.get_event_loop().run_in_executor(None, _run_claude, _deep_prompt(indexed, profile_text))
        parsed = _safe_parse(raw)
        for item in parsed:
            local_rank = item.get("rank")
            if local_rank is not None and bs + local_rank < len(deep_input):
                item["orig_index"] = deep_input[bs + local_rank]["orig_index"]
        deep_results.extend(parsed)

    for item in deep_results:
        orig_idx = item.get("orig_index")
        if orig_idx is not None and 0 <= orig_idx < len(enriched):
            if "relevance_score" in item:
                enriched[orig_idx].relevance_score = float(item["relevance_score"])
            enriched[orig_idx].relevance_reasoning = item.get("relevance_reasoning", "")
            enriched[orig_idx].brief = item.get("brief", "")
            if item.get("category"):
                enriched[orig_idx].category = item["category"]

    _info(f"[claude] stage 2 done — {len(deep_results)} briefs")

    # ── STAGE 3: Strategy Memo ────────────────────────────────────────────
    strategy_indices = sorted(range(len(enriched)), key=lambda i: -enriched[i].relevance_score)[:TOP_N_STRATEGY]
    _info(f"[claude] stage 3 — strategy memos for top {len(strategy_indices)}")

    for rank, orig_idx in enumerate(strategy_indices):
        g = enriched[orig_idx]
        _info(f"[claude] memo {rank+1}/{len(strategy_indices)}: {g.title[:55]}")
        raw = await asyncio.get_event_loop().run_in_executor(
            None, _run_claude, _strategy_prompt(g, profile_text, inst)
        )
        memo = raw.strip()
        if len(memo) > 80:
            enriched[orig_idx].strategy_memo = memo

    _info(f"[claude] stage 3 done")

    # ── Deadlines ─────────────────────────────────────────────────────────
    from datetime import datetime
    today = datetime.now()
    for g in enriched:
        if g.deadline:
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%B %d, %Y"):
                try:
                    g.days_until_deadline = (datetime.strptime(g.deadline, fmt) - today).days
                    break
                except ValueError:
                    pass

    return sorted(enriched, key=lambda x: -x.relevance_score)


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_profile_text(profile: ClientProfileInput, inst: dict) -> str:
    lines = [
        f"Organisation: {profile.name}",
        f"Type: {profile.institution_type}",
        f"Research areas: {', '.join(profile.research_areas)}",
        f"Keywords: {', '.join(profile.keywords)}",
        f"Focus geographies: {', '.join(profile.focus_geographies)}",
        f"Eligibility notes: {', '.join(profile.eligibility_notes)}",
    ]
    if inst.get("concepts"):
        lines.append(f"Verified research strengths (from publication record): {', '.join(inst['concepts'][:6])}")
    if inst.get("past_funders"):
        lines.append(f"Past funders (from publication acknowledgements): {', '.join(inst['past_funders'][:5])}")
    if inst.get("works_count"):
        lines.append(f"Publication record: {inst['works_count']:,} works, {inst.get('cited_by_count', 0):,} total citations")
    return "\n".join(lines)


def _screen_prompt(batch: list[dict], profile_text: str) -> str:
    return f"""You are a grant screening analyst. Score each grant for relevance to this client. Return ONLY a JSON array, no markdown.

CLIENT:
{profile_text}

GRANTS:
{json.dumps(batch, indent=2)}

Return one object per grant:
- "index": integer from input
- "relevance_score": float 0.0–1.0 (be discriminating — most grants should score below 0.5)
- "category": health|education|environment|technology|agriculture|development|social|other

Return ONLY the JSON array. Start with [ end with ]."""


def _deep_prompt(batch: list[dict], profile_text: str) -> str:
    return f"""You are a senior grant intelligence analyst with deep expertise in research funding. Write authoritative, specific intelligence briefs — not generic summaries.

CLIENT PROFILE:
{profile_text}

GRANTS (with real funder award data where available):
{json.dumps(batch, indent=2)}

For EACH grant return one JSON object:
- "rank": integer from input
- "relevance_score": refined float 0.0–1.0 (use funder_intel to adjust — if India awards exist, score higher)
- "relevance_reasoning": ONE precise sentence naming the specific alignment (cite research areas, not just topic)
- "brief": 260–300 word expert brief structured as:
  FUNDER PRIORITIES: What this agency specifically funds and values, based on their award history.
  CLIENT FIT: Exactly why THIS institution is well positioned — cite specific research strengths.
  KEY REQUIREMENTS: The 3 most important eligibility/application requirements.
  STRATEGIC NOTE: Whether to apply solo or with partner, what preliminary data is needed, timing advice.
- "category": health|education|environment|technology|agriculture|development|social|other

Return ONLY the JSON array. Start with [ end with ]."""


def _strategy_prompt(grant: GrantInput, profile_text: str, inst: dict) -> str:
    researchers = inst.get("top_researchers", [])
    researcher_line = ""
    if researchers:
        researcher_line = "\nKey researchers: " + ", ".join(f"{r['name']} ({r['concept']})" for r in researchers[:4])

    funder_section = f"\nFUNDER INTELLIGENCE:\n{grant.funder_intel}" if grant.funder_intel else ""

    return f"""You are a senior grant strategy advisor. Write a direct, actionable "How to Win" memo for a research team preparing this application.

CLIENT PROFILE:
{profile_text}{researcher_line}

TARGET GRANT:
Title: {grant.title}
Agency: {grant.agency}
Funding: {grant.funding_amount or 'unspecified'}
Deadline: {grant.deadline or 'unspecified'}
Eligibility: {grant.eligibility or 'unspecified'}
Description: {grant.description[:500]}{funder_section}

Write a 360–400 word strategy memo in plain text (no JSON, no markdown symbols). Use these section headers exactly:

THE OPPORTUNITY
Why this grant is worth pursuing for this team right now, in 2–3 sentences.

YOUR STRONGEST ANGLE
The specific narrative that will resonate with this funder. Name what aspect of the client's work should lead the application and why the funder will care.

THREE CRITICAL REQUIREMENTS
The 3 non-negotiable things to get right (collaborator needed, preliminary data, eligibility proof, framing). Be specific.

NEXT 30 DAYS
Three concrete actions the team should take immediately. Name a specific person/platform/action for each.

Write directly to the team. No fluff, no hedging."""


# ── Runtime helpers ───────────────────────────────────────────────────────────

def _run_claude(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            _warn(f"[claude] stderr: {result.stderr[:200]}")
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "[]"
    except FileNotFoundError:
        raise RuntimeError("claude CLI not found. Ensure Claude Code is installed and on PATH.")


def _safe_parse(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        _warn(f"[claude] JSON parse failed. Snippet: {text[:200]}")
        return []
