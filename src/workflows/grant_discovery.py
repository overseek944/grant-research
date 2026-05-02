"""
Temporal workflow: parallel grant discovery across all sources → Claude analysis → HTML report.
"""
import asyncio
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from ..models import WorkflowParams, WorkflowResult, GrantInput
    from ..activities.grants_gov import search_grants_gov
    from ..activities.nih_reporter import search_nih
    from ..activities.nsf import search_nsf
    from ..activities.eu_horizon import search_eu_horizon
    from ..activities.india_grants import search_india_grants
    from ..activities.open_alex import get_institution_profile
    from ..activities.ai_analysis import analyze_grants_with_claude
    from ..report.generator import generate_report_activity

ACTIVITY_TIMEOUT = timedelta(seconds=90)
RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=5))


@workflow.defn
class GrantDiscoveryWorkflow:
    @workflow.run
    async def run(self, params: WorkflowParams) -> WorkflowResult:
        profile = params.profile
        max_r = params.max_results_per_source

        workflow.logger.info(f"Starting grant discovery for: {profile.name}")

        # ── Step 1: Institution profile + search all sources in parallel ──
        inst_profile_task = workflow.execute_activity(
            get_institution_profile,
            args=[profile.name],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        search_tasks = [
            workflow.execute_activity(
                search_grants_gov,
                args=[profile, max_r],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=RETRY,
            ),
            workflow.execute_activity(
                search_nih,
                args=[profile, max_r],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=RETRY,
            ),
            workflow.execute_activity(
                search_nsf,
                args=[profile, max_r],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=RETRY,
            ),
            workflow.execute_activity(
                search_eu_horizon,
                args=[profile, max_r],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=RETRY,
            ),
            workflow.execute_activity(
                search_india_grants,
                args=[profile, max_r],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=RETRY,
            ),
        ]
        all_tasks = [inst_profile_task] + search_tasks
        all_results = await asyncio.gather(*all_tasks, return_exceptions=True)
        institution_profile = all_results[0] if not isinstance(all_results[0], Exception) else {}
        results = all_results[1:]

        all_grants: list[GrantInput] = []
        sources_searched: list[str] = []
        for source_name, res in zip(
            ["grants_gov", "nih", "nsf", "eu_horizon", "india_grants"], results
        ):
            if isinstance(res, Exception):
                workflow.logger.warning(f"Source {source_name} failed: {res}")
            else:
                all_grants.extend(res)
                sources_searched.append(source_name)

        workflow.logger.info(f"Total raw grants collected: {len(all_grants)}")

        # ── Step 2: Deduplicate by title similarity ────────────────────────
        all_grants = _deduplicate(all_grants)
        workflow.logger.info(f"After dedup: {len(all_grants)}")

        # ── Step 3: Claude 3-stage analysis (screen → funder intel → deep → strategy)
        analysed: list[GrantInput] = await workflow.execute_activity(
            analyze_grants_with_claude,
            args=[all_grants, profile, institution_profile],
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

        # ── Step 4: Segment by relevance ──────────────────────────────────
        highly_relevant = [g for g in analysed if g.relevance_score >= 0.65]
        relevant = [g for g in analysed if 0.35 <= g.relevance_score < 0.65]
        upcoming = sorted(
            [g for g in analysed if g.days_until_deadline is not None and 0 <= g.days_until_deadline <= 45],
            key=lambda x: x.days_until_deadline,
        )

        # ── Step 5: Generate HTML report ──────────────────────────────────
        from datetime import datetime
        report_path: str = await workflow.execute_activity(
            generate_report_activity,
            args=[analysed, profile, sources_searched, institution_profile],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RETRY,
        )

        return WorkflowResult(
            generated_at=datetime.now().isoformat(),
            client_name=profile.name,
            total_found=len(analysed),
            highly_relevant=highly_relevant,
            relevant=relevant,
            upcoming=upcoming,
            sources_searched=sources_searched,
            report_path=report_path,
        )


def _deduplicate(grants: list[GrantInput]) -> list[GrantInput]:
    seen: set[str] = set()
    unique: list[GrantInput] = []
    for g in grants:
        key = g.title[:50].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(g)
    return unique
