from __future__ import annotations
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field


class GrantOpportunity(BaseModel):
    id: str
    title: str
    agency: str
    description: str
    deadline: Optional[str] = None         # ISO date string or human-readable
    funding_amount: Optional[str] = None
    eligibility: Optional[str] = None
    url: str
    source: str                             # grants_gov | nih | nsf | eu_horizon | birac | india_gov
    relevance_score: float = 0.0            # 0.0–1.0, set by Claude
    relevance_reasoning: str = ""
    brief: str = ""                         # 150–200 word brief from Claude
    category: str = ""                      # health | education | environment | technology | development | other
    days_until_deadline: Optional[int] = None


class ClientProfile(BaseModel):
    name: str = "Research Institution"
    institution_type: str = "university"    # university | ngo | hospital | social_enterprise
    research_areas: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    eligibility_notes: list[str] = Field(default_factory=list)
    focus_geographies: list[str] = Field(default_factory=list)
    max_funding_needed: Optional[str] = None
    url: Optional[str] = None               # programme/institution URL for web enrichment


# ── Temporal-serialisable dataclasses (JSON-friendly) ──────────────────────

@dataclass
class ClientProfileInput:
    name: str
    institution_type: str
    research_areas: list[str]
    keywords: list[str]
    eligibility_notes: list[str]
    focus_geographies: list[str]
    max_funding_needed: Optional[str]
    url: Optional[str] = None               # programme/institution URL for web enrichment

    @classmethod
    def from_profile(cls, p: ClientProfile) -> "ClientProfileInput":
        return cls(
            name=p.name,
            institution_type=p.institution_type,
            research_areas=p.research_areas,
            keywords=p.keywords,
            eligibility_notes=p.eligibility_notes,
            focus_geographies=p.focus_geographies,
            max_funding_needed=p.max_funding_needed,
            url=p.url,
        )


@dataclass
class GrantInput:
    id: str
    title: str
    agency: str
    description: str
    deadline: Optional[str]
    funding_amount: Optional[str]
    eligibility: Optional[str]
    url: str
    source: str
    relevance_score: float = 0.0
    relevance_reasoning: str = ""
    brief: str = ""
    category: str = ""
    days_until_deadline: Optional[int] = None
    funder_intel: str = ""      # actual award history injected by funder_intel activity
    strategy_memo: str = ""     # "how to win" memo for top 5 grants

    @classmethod
    def from_opportunity(cls, o: GrantOpportunity) -> "GrantInput":
        return cls(**o.model_dump())

    def to_opportunity(self) -> GrantOpportunity:
        return GrantOpportunity(**self.__dict__)


@dataclass
class WorkflowParams:
    profile: ClientProfileInput
    max_results_per_source: int = 30


@dataclass
class WorkflowResult:
    generated_at: str
    client_name: str
    total_found: int
    highly_relevant: list[GrantInput]
    relevant: list[GrantInput]
    upcoming: list[GrantInput]
    sources_searched: list[str]
    report_path: str
