from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib

from .domain import Proposal
from .relations import LINK_PREP_SUBTASK, LINK_TYPE_KEY, NEEDS_PREP_KEY
from .source_refs import text_hash


def routine_occurrence_date(proposal: Proposal) -> date | None:
    """Pure: derive the routine occurrence date a prep subtask hangs off of."""

    raw = proposal.metadata.get("next_occurrence_date") or proposal.metadata.get("routine_occurrence_date")
    if raw:
        return date.fromisoformat(raw)
    if proposal.scheduled_date is not None:
        return proposal.scheduled_date
    return None


def build_prep_subtasks(parent: Proposal, *, now: datetime) -> tuple[Proposal, ...]:
    """Pure construction of the prep child proposal(s) for a routine parent.

    Returns the prep ``Proposal`` objects without persisting them or emitting
    events; the orchestrator owns the store-dependent dedup check, the
    ``save_proposal`` calls, and the lifecycle events.  The proposal_id is
    derived deterministically (``sha1(parent_id:prep:occurrence)``) so a given
    parent + occurrence always yields the same child id.
    """

    if parent.kind != "routine" or parent.metadata.get(NEEDS_PREP_KEY) != "true":
        return ()
    occurrence_date = routine_occurrence_date(parent)
    if occurrence_date is None:
        return ()
    prep_due = occurrence_date - timedelta(days=1)
    assignee = parent.proposer_id if parent.proposer_id in {"me", "teammate"} else "me"
    digest = hashlib.sha1(
        f"{parent.proposal_id}:prep:{occurrence_date.isoformat()}".encode("utf-8")
    ).hexdigest()[:12]
    prep = Proposal(
        proposal_id=f"{parent.proposal_id}/prep/{digest}",
        source_message_id=parent.source_message_id,
        proposer_id=parent.proposer_id,
        title=f"{parent.title} 자료 준비",
        raw_text=f"{parent.raw_text} / 자료 준비",
        kind="task",
        status="approved",
        assigned_to=assignee,
        task_management_area=parent.task_management_area,
        discussion_id=parent.discussion_id,
        message_id=f"{parent.message_id}/prep",
        required_approvers=(assignee,),
        approvals=(assignee,),
        missing_slots=(),
        due_date=prep_due,
        created_at=now,
        updated_at=now,
        metadata={
            "parent_proposal_id": parent.proposal_id,
            LINK_TYPE_KEY: LINK_PREP_SUBTASK,
            "routine_occurrence_date": occurrence_date.isoformat(),
            "due_offset_days": "1",
            "materials": parent.metadata.get("materials", "자료 준비"),
            "source_text_hash": text_hash(parent.raw_text),
        },
    )
    return (prep,)
