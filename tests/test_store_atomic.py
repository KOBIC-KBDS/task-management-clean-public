from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from task_management.domain import Proposal
from task_management.store import TeamTaskStore
import task_management.store as store_module


NOW = datetime(2026, 7, 30, 14, 0, 0)


def test_save_proposals_atomic_rolls_back_entire_batch_on_failure(tmp_path: Path, monkeypatch) -> None:
    store = TeamTaskStore(tmp_path / "state.sqlite3", tmp_path / "events.jsonl")
    first = _proposal("proposal/first", "First")
    second = _proposal("proposal/second", "Second")
    store.save_proposal(first)
    store.save_proposal(second)

    real_upsert = store_module._upsert_proposal
    calls = 0

    def fail_second_upsert(conn, proposal):
        nonlocal calls
        calls += 1
        real_upsert(conn, proposal)
        if calls == 2:
            raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(store_module, "_upsert_proposal", fail_second_upsert)

    with pytest.raises(RuntimeError, match="injected transaction failure"):
        store.save_proposals_atomic(
            (
                replace(first, title="Changed first", updated_at=NOW),
                replace(second, title="Changed second", updated_at=NOW),
            )
        )

    assert store.get_proposal(first.proposal_id).title == first.title  # type: ignore[union-attr]
    assert store.get_proposal(second.proposal_id).title == second.title  # type: ignore[union-attr]


def test_save_proposals_with_audit_recovers_pending_outbox_after_log_failure(tmp_path: Path) -> None:
    store = TeamTaskStore(tmp_path / "state.sqlite3", tmp_path / "events.jsonl")
    proposal = _proposal("proposal/recoverable", "Recoverable")
    store.save_proposal(proposal)
    invalid_event_path = tmp_path / "event-log-directory"
    invalid_event_path.mkdir()
    store.event_log_path = invalid_event_path

    store.save_proposals_with_audit_atomic(
        (replace(proposal, title="Committed", updated_at=NOW),),
        (("proposal.changed", {"proposal_id": proposal.proposal_id}, NOW),),
    )

    committed = store.get_proposal(proposal.proposal_id)
    assert committed is not None
    assert committed.title == "Committed"
    with pytest.raises(IsADirectoryError):
        store.flush_audit_outbox()

    store.event_log_path = tmp_path / "recovered-events.jsonl"
    assert store.flush_audit_outbox() == 1
    events = store.read_events()
    assert [event["type"] for event in events] == ["proposal.changed"]
    assert isinstance(events[0]["outbox_id"], int)
    assert store.flush_audit_outbox() == 0


def _proposal(proposal_id: str, title: str) -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        source_message_id=f"source/{proposal_id}",
        proposer_id="me",
        title=title,
        raw_text=title,
        kind="task",
        status="approved",
        assigned_to="me",
        task_management_area="general",
        discussion_id="private/test",
        message_id=f"message/{proposal_id}",
        required_approvers=("me",),
        approvals=("me",),
        created_at=NOW,
        updated_at=NOW,
    )
