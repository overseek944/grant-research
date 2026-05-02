"""
HTML report generator.
Called both as a Temporal activity and directly in demo mode.
"""
import re
from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup
from temporalio import activity

from ..models import GrantInput, ClientProfileInput

TEMPLATE_DIR = Path(__file__).parent / "templates"
OUTPUT_DIR = Path(__file__).parent.parent.parent.parent / "outputs"


@activity.defn
async def generate_report_activity(
    grants: list[GrantInput],
    profile: ClientProfileInput,
    sources_searched: list[str],
    institution_profile: dict = None,
) -> str:
    return generate_report(grants, profile, sources_searched, institution_profile or {})


def generate_report(
    grants: list[GrantInput],
    profile: ClientProfileInput,
    sources_searched: list[str],
    institution_profile: dict = None,
) -> str:
    """Generate HTML report and return the file path."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    inst = institution_profile or {}

    highly_relevant = sorted([g for g in grants if g.relevance_score >= 0.65], key=lambda x: -x.relevance_score)
    relevant = sorted([g for g in grants if 0.35 <= g.relevance_score < 0.65], key=lambda x: -x.relevance_score)
    low_relevance = [g for g in grants if g.relevance_score < 0.35]

    upcoming = sorted(
        [g for g in grants if g.days_until_deadline is not None and 0 <= g.days_until_deadline <= 60],
        key=lambda x: x.days_until_deadline,
    )

    cats: dict[str, int] = {}
    for g in grants:
        c = g.category or "other"
        cats[c] = cats.get(c, 0) + 1

    source_labels = {
        "grants_gov": "Grants.gov (US Federal)",
        "nih": "NIH Reporter",
        "nsf": "NSF Awards",
        "eu_horizon": "EU Horizon Europe",
        "birac": "BIRAC (India)",
        "dst": "DST / SERB (India)",
        "dbt": "DBT (India)",
        "icmr": "ICMR (India)",
        "aim": "Atal Innovation Mission",
        "csir": "CSIR (India)",
        "foundations": "Private Foundations",
        "india_grants": "India Grants (Combined)",
        "openalex": "OpenAlex Research Graph",
    }

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    env.filters["source_label"] = lambda s: source_labels.get(s, s.replace("_", " ").title())
    env.filters["memo_html"] = _memo_to_html
    env.filters["intel_html"] = _intel_to_html

    template = env.get_template("report.html")
    html = template.render(
        generated_at=datetime.now().strftime("%B %d, %Y at %H:%M"),
        client=profile,
        institution_profile=inst,
        highly_relevant=highly_relevant,
        relevant=relevant,
        low_relevance=low_relevance,
        upcoming=upcoming,
        total=len(grants),
        sources_searched=sources_searched,
        source_labels=source_labels,
        categories=cats,
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = profile.name.replace(" ", "_").replace("/", "-")[:30]
    out_path = OUTPUT_DIR / f"grant_report_{safe_name}_{ts}.html"
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)


def _memo_to_html(text: str) -> str:
    """Convert strategy memo plain text with section headers to styled HTML."""
    if not text:
        return ""
    lines = text.split("\n")
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append("<br>")
            continue
        # Section headers (ALL CAPS lines or lines ending with nothing after colon)
        if re.match(r"^[A-Z][A-Z\s&/]{4,}$", stripped) or re.match(r"^(THE OPPORTUNITY|YOUR STRONGEST ANGLE|THREE CRITICAL REQUIREMENTS|NEXT 30 DAYS)$", stripped):
            out.append(f'<p class="text-xs font-bold text-slate-600 uppercase tracking-widest mt-4 mb-1">{stripped}</p>')
        elif stripped.startswith("• ") or stripped.startswith("- "):
            out.append(f'<p class="text-sm text-slate-700 leading-relaxed pl-3 border-l-2 border-indigo-200 mb-1">{stripped[2:]}</p>')
        elif re.match(r"^\d+\.", stripped):
            out.append(f'<p class="text-sm text-slate-700 leading-relaxed mb-1 font-medium">{stripped}</p>')
        else:
            out.append(f'<p class="text-sm text-slate-700 leading-relaxed mb-1">{stripped}</p>')
    return Markup("\n".join(out))


def _intel_to_html(text: str) -> str:
    """Format funder intelligence text with bullet alignment."""
    if not text:
        return ""
    lines = text.split("\n")
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("  •") or stripped.startswith("•"):
            content = stripped.lstrip("• ").strip()
            out.append(f'<li class="text-xs text-slate-600 leading-relaxed">{content}</li>')
        elif "—" in stripped and ":" not in stripped[:30]:
            out.append(f'<p class="text-xs font-semibold text-slate-500 uppercase tracking-wide mt-2 mb-1">{stripped}</p>')
        else:
            # Section header line
            out.append(f'<p class="text-xs font-semibold text-slate-600 mt-2 mb-1">{stripped}</p>')
    result = []
    in_list = False
    for item in out:
        if item.startswith("<li"):
            if not in_list:
                result.append('<ul class="list-none space-y-0.5 ml-2">')
                in_list = True
        else:
            if in_list:
                result.append("</ul>")
                in_list = False
        result.append(item)
    if in_list:
        result.append("</ul>")
    return Markup("\n".join(result))
