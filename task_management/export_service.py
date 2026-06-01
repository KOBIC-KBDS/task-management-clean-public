from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .orchestrator import TeamTaskOrchestrator
from .store import TeamTaskStore
from .task_core_bridge import build_task_management_task_export_from_proposals, validate_with_task_core


@dataclass(frozen=True)
class ExportPreviewResult:
    payload: dict[str, Any]
    preview: dict[str, Any]
    applied_proposal_ids: tuple[str, ...] = ()


def preview_approved_proposals(
    store: TeamTaskStore,
    *,
    exported_at: datetime | None = None,
    task_core_root: str | Path | None = None,
) -> ExportPreviewResult:
    payload = build_task_management_task_export_from_proposals(
        store.list_proposals(),
        exported_at=exported_at,
    )
    preview = validate_with_task_core(payload, root=task_core_root)
    return ExportPreviewResult(payload=payload, preview=preview)


def export_and_mark_approved_proposals_applied(
    store: TeamTaskStore,
    *,
    exported_at: datetime | None = None,
    task_core_root: str | Path | None = None,
) -> ExportPreviewResult:
    now = exported_at or datetime.now()
    result = preview_approved_proposals(store, exported_at=now, task_core_root=task_core_root)
    if not result.preview.get("ok") or result.preview.get("restores") is not False:
        return result

    orchestrator = TeamTaskOrchestrator(store)
    applied: list[str] = []
    for item in result.payload["items"]:
        proposal_id = item.get("metadata", {}).get("proposal_id")
        if not proposal_id:
            continue
        updated = orchestrator.mark_applied(proposal_id, str(item.get("id", "")), applied_at=now)
        if updated is not None:
            applied.append(proposal_id)
    return ExportPreviewResult(
        payload=result.payload,
        preview=result.preview,
        applied_proposal_ids=tuple(applied),
    )
