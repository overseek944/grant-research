#!/usr/bin/env python3
"""
Grant Research Agent — Entry Point

Usage:
  # Quick demo (no Temporal server needed):
  python run.py --demo

  # Full Temporal workflow:
  # Terminal 1: temporal server start-dev
  # Terminal 2: python run.py --worker
  # Terminal 3: python run.py

  # Custom profile:
  python run.py --demo --profile config/my_client.yaml

  # Open report in browser after generating:
  python run.py --demo --open
"""
import argparse
import asyncio
import sys
import webbrowser
from pathlib import Path

import yaml


def load_profile(profile_path: str):
    from src.models import ClientProfile
    path = Path(profile_path)
    if not path.exists():
        print(f"[error] Profile not found: {profile_path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        data = yaml.safe_load(f)
    return ClientProfile(**data)


# ── DEMO MODE (no Temporal, runs pipeline directly) ──────────────────────

async def run_demo(profile_path: str, open_browser: bool = False):
    print("=" * 60)
    print("  Grant Intelligence Agent — Demo Mode")
    print("=" * 60)

    from src.models import ClientProfile, ClientProfileInput
    profile_obj = load_profile(profile_path)
    profile = ClientProfileInput.from_profile(profile_obj)

    print(f"\n  Client:      {profile.name}")
    print(f"  Type:        {profile.institution_type}")
    print(f"  Areas:       {', '.join(profile.research_areas[:4])}")
    print(f"  Keywords:    {', '.join(profile.keywords[:5])}")
    print()

    # Import activities directly
    from src.activities.grants_gov import search_grants_gov
    from src.activities.nih_reporter import search_nih
    from src.activities.nsf import search_nsf
    from src.activities.eu_horizon import search_eu_horizon
    from src.activities.india_grants import search_india_grants
    from src.activities.open_alex import get_institution_profile
    from src.activities.web_profile import enrich_profile_from_web
    from src.activities.funder_intel import enrich_with_funder_intel
    from src.activities.ai_analysis import analyze_grants_with_claude
    from src.report.generator import generate_report

    import logging
    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    # ── Web enrichment: discover real research keywords before searching ──
    print("  [0/4] Enriching profile from web search...")
    web_data = await _safe_dict(enrich_profile_from_web, profile, label="Web enrichment")
    if web_data.get("keywords"):
        old_kw_count = len(profile.keywords)
        # Merge: web keywords take precedence, keep any YAML-only ones not already covered
        merged_kw = web_data["keywords"]
        for kw in profile.keywords:
            if kw.lower() not in {k.lower() for k in merged_kw}:
                merged_kw.append(kw)
        profile.keywords = merged_kw[:15]
        if web_data.get("research_areas"):
            profile.research_areas = web_data["research_areas"]
        summary = web_data.get("web_summary", "")
        print(f"      ✓ Web-enriched: {len(merged_kw)} keywords (was {old_kw_count})")
        print(f"        Keywords: {', '.join(profile.keywords[:6])}…")
        if summary:
            print(f"        Summary: {summary}")
    else:
        print(f"      ⚠ Web enrichment skipped — using YAML profile as-is")

    # ── Phase A: OpenAlex profile — sequential so concepts enrich keywords ──
    print("  [1/5] Building institution research profile (OpenAlex)...")
    institution_profile = await _safe_dict(get_institution_profile, profile.name, label="OpenAlex")
    if institution_profile.get("concepts"):
        _merge_openalex_keywords(profile, institution_profile["concepts"])
        print(f"      ✓ Research fingerprint: {', '.join(institution_profile['concepts'][:4])}…")
        print(f"        Keywords now ({len(profile.keywords)}): {', '.join(profile.keywords[:8])}…")
    else:
        print(f"      ⚠ OpenAlex: institution not found — using web-enriched keywords only")

    # ── Phase B: Grant searches — now use fully enriched keyword set ────────
    print("  [2/5] Searching grant databases with enriched profile...")
    results = await asyncio.gather(
        _safe_run(search_grants_gov, profile, 25, label="Grants.gov"),
        _safe_run(search_nih, profile, 20, label="NIH"),
        _safe_run(search_nsf, profile, 20, label="NSF"),
        _safe_run(search_eu_horizon, profile, 20, label="EU Horizon"),
        _safe_run(search_india_grants, profile, 25, label="India Grants"),
    )

    all_grants = []
    sources_found = []
    source_names = ["grants_gov", "nih", "nsf", "eu_horizon", "india_grants"]
    for name, batch in zip(source_names, results):
        if batch:
            all_grants.extend(batch)
            sources_found.append(name)
            print(f"      ✓ {name}: {len(batch)} results")
        else:
            print(f"      ✗ {name}: no results / error")

    # Deduplicate
    seen: set[str] = set()
    unique = []
    for g in all_grants:
        key = g.title[:50].lower()
        if key not in seen:
            seen.add(key)
            unique.append(g)

    print(f"\n  [3/5] Collected {len(unique)} unique opportunities (deduped from {len(all_grants)})")

    print("\n  [4/5] Running 3-stage Claude analysis (screen → funder intel → deep briefs → strategy memos)...")
    print("        This takes 5–9 minutes. Pipeline: 2 screen batches → 3 deep batches → 5 strategy memos.")
    try:
        analysed = await analyze_grants_with_claude(unique, profile, institution_profile)
        high = [g for g in analysed if g.relevance_score >= 0.65]
        mid = [g for g in analysed if 0.35 <= g.relevance_score < 0.65]
        with_strategy = [g for g in analysed if g.strategy_memo]
        print(f"      ✓ {len(high)} high-priority, {len(mid)} worth reviewing, {len(with_strategy)} with strategy memos")
    except Exception as e:
        print(f"      ⚠ Claude analysis skipped: {e}")
        analysed = unique

    print("\n  [5/5] Generating HTML report...")
    report_path = generate_report(analysed, profile_obj, sources_found, institution_profile)
    print(f"      ✓ Report saved: {report_path}")

    # ── Push structured JSON to grant-runs repo (if configured) ──────────
    import os
    grant_runs_path = os.getenv("GRANT_RUNS_PATH", "")
    if grant_runs_path:
        try:
            from src.report.json_writer import write_run_json
            from src.report.git_manager import push_to_grant_runs
            run_data = write_run_json(analysed, profile_obj, sources_found, institution_profile)
            pushed = push_to_grant_runs(run_data, profile.name, grant_runs_path)
            if pushed:
                print(f"      ✓ Data pushed to grant-runs ({grant_runs_path})")
            else:
                print(f"      ⚠ grant-runs push failed (check logs)")
        except Exception as e:
            print(f"      ⚠ grant-runs push skipped: {e}")

    print()
    print("=" * 60)
    print(f"  Done! Report: {report_path}")
    print("=" * 60)

    if open_browser:
        webbrowser.open(f"file://{report_path}")
        print("  Opened in browser.")

    return report_path


async def _safe_run(fn, *args, label=""):
    """Run an activity function, returning [] on any error."""
    try:
        return await fn(*args)
    except Exception as e:
        print(f"      ✗ {label}: {type(e).__name__}: {e}")
        return []


async def _safe_dict(fn, *args, label=""):
    """Run a function that returns a dict, returning {} on any error."""
    try:
        return await fn(*args)
    except Exception as e:
        print(f"      ✗ {label}: {type(e).__name__}: {e}")
        return {}


def _merge_openalex_keywords(profile, concepts: list[str]):
    """Add OpenAlex publication-derived concepts into profile keywords (no duplicates)."""
    existing = {k.lower() for k in profile.keywords}
    new_kw = [c for c in concepts if c.lower() not in existing]
    profile.keywords = (profile.keywords + new_kw)[:20]


# ── WORKER MODE ──────────────────────────────────────────────────────────

async def run_worker(temporal_address: str):
    from src.worker import run_worker as _run_worker
    await _run_worker(temporal_address)


# ── TEMPORAL TRIGGER MODE ─────────────────────────────────────────────────

async def run_temporal_workflow(profile_path: str, temporal_address: str, open_browser: bool):
    from temporalio.client import Client
    from src.models import ClientProfileInput, WorkflowParams
    from src.workflows.grant_discovery import GrantDiscoveryWorkflow

    profile_obj = load_profile(profile_path)
    profile = ClientProfileInput.from_profile(profile_obj)

    print(f"Connecting to Temporal at {temporal_address}...")
    client = await Client.connect(temporal_address)

    print(f"Starting GrantDiscoveryWorkflow for: {profile.name}")
    result = await client.execute_workflow(
        GrantDiscoveryWorkflow.run,
        WorkflowParams(profile=profile),
        id=f"grant-discovery-{profile.name[:20].replace(' ', '-')}",
        task_queue="grant-research",
    )

    print(f"\nWorkflow complete!")
    print(f"  Total opportunities found: {result.total_found}")
    print(f"  High priority:  {len(result.highly_relevant)}")
    print(f"  Worth review:   {len(result.relevant)}")
    print(f"  Upcoming (45d): {len(result.upcoming)}")
    print(f"  Report:         {result.report_path}")

    if open_browser and result.report_path:
        webbrowser.open(f"file://{result.report_path}")


# ── MAIN ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Grant Research Agent")
    parser.add_argument("--demo", action="store_true", help="Run without Temporal (demo mode)")
    parser.add_argument("--worker", action="store_true", help="Start Temporal worker")
    parser.add_argument("--profile", default="config/default_profile.yaml", help="Path to client profile YAML")
    parser.add_argument("--temporal", default="localhost:7233", help="Temporal server address")
    parser.add_argument("--open", action="store_true", help="Open report in browser when done")
    args = parser.parse_args()

    # Change to project root so relative paths work
    import os
    os.chdir(Path(__file__).parent)

    if args.demo:
        asyncio.run(run_demo(args.profile, open_browser=args.open))
    elif args.worker:
        print(f"Starting Temporal worker (connecting to {args.temporal})...")
        asyncio.run(run_worker(args.temporal))
    else:
        asyncio.run(run_temporal_workflow(args.profile, args.temporal, args.open))


if __name__ == "__main__":
    main()
