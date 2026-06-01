from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable

from .domain import Proposal


@dataclass(frozen=True)
class BackfillSuggestion:
    suggested_parent_title: str
    child_proposal_ids: tuple[str, ...]
    confidence: float
    evidence: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "suggested_parent_title": self.suggested_parent_title,
            "child_proposal_ids": list(self.child_proposal_ids),
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }


def build_workflow_backfill_report(
    proposals: Iterable[Proposal],
    events: Iterable[dict[str, Any]],
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return read-only hierarchy suggestions for existing flat proposals."""

    proposal_tuple = tuple(proposals)
    event_tuple = tuple(events)
    suggestions = _suggest_by_source_message(proposal_tuple)
    high_confidence = [item for item in suggestions if item.confidence >= 0.8]
    return {
        "schema": "task-management.workflow-backfill-report.v1",
        "generated_at": (generated_at or datetime.now()).isoformat(timespec="seconds"),
        "mutation_policy": "preview_only_no_store_writes_no_event_append_no_outbound",
        "counts": {
            "proposals_scanned": len(proposal_tuple),
            "events_scanned": len(event_tuple),
            "suggestions_generated": len(suggestions),
            "high_confidence_suggestions": len(high_confidence),
            "skipped_ambiguous_items": max(0, len(proposal_tuple) - sum(len(item.child_proposal_ids) for item in suggestions)),
        },
        "suggestions": [item.to_payload() for item in suggestions],
    }


def write_workflow_backfill_report(report: dict[str, Any], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _suggest_by_source_message(proposals: tuple[Proposal, ...]) -> tuple[BackfillSuggestion, ...]:
    groups: dict[str, list[Proposal]] = {}
    for proposal in proposals:
        if proposal.metadata.get("parent_proposal_id"):
            continue
        if proposal.status in {"rejected", "done"}:
            continue
        groups.setdefault(proposal.source_message_id or proposal.message_id, []).append(proposal)

    suggestions: list[BackfillSuggestion] = []
    for source_message_id, items in groups.items():
        if len(items) < 2:
            continue
        ordered = sorted(items, key=lambda item: (item.scheduled_date or item.due_date or datetime.max.date(), item.time_window, item.title))
        suggestions.append(
            BackfillSuggestion(
                suggested_parent_title=_common_parent_title(ordered),
                child_proposal_ids=tuple(item.proposal_id for item in ordered),
                confidence=0.82,
                evidence=(
                    f"shared_source_message_id:{source_message_id}",
                    f"candidate_count:{len(ordered)}",
                    "preview_only_no_mutation",
                ),
            )
        )
    return tuple(suggestions)


def _common_parent_title(proposals: tuple[Proposal, ...] | list[Proposal]) -> str:
    if not proposals:
        return "Workflow"
    first = proposals[0].title
    tokens = first.split()
    for size in range(min(4, len(tokens)), 0, -1):
        prefix = " ".join(tokens[:size])
        if all(item.title.startswith(prefix) for item in proposals):
            return prefix
    return f"{proposals[0].title} workflow"
