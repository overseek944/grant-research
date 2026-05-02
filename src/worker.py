"""
Temporal worker — registers all activities and workflows, then polls for tasks.
Run this in one terminal while run.py triggers workflow in another.
"""
import asyncio
from temporalio.client import Client
from temporalio.worker import Worker

from .activities.grants_gov import search_grants_gov
from .activities.nih_reporter import search_nih
from .activities.nsf import search_nsf
from .activities.eu_horizon import search_eu_horizon
from .activities.india_grants import search_india_grants
from .activities.open_alex import get_institution_profile
from .activities.funder_intel import enrich_with_funder_intel
from .activities.ai_analysis import analyze_grants_with_claude
from .report.generator import generate_report_activity
from .workflows.grant_discovery import GrantDiscoveryWorkflow

TASK_QUEUE = "grant-research"


async def run_worker(temporal_address: str = "localhost:7233"):
    client = await Client.connect(temporal_address)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[GrantDiscoveryWorkflow],
        activities=[
            search_grants_gov,
            search_nih,
            search_nsf,
            search_eu_horizon,
            search_india_grants,
            get_institution_profile,
            enrich_with_funder_intel,
            analyze_grants_with_claude,
            generate_report_activity,
        ],
    )
    print(f"[worker] connected to Temporal at {temporal_address}")
    print(f"[worker] listening on task queue: {TASK_QUEUE}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(run_worker())
