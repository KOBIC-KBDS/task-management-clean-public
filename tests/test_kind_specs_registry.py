from __future__ import annotations

from task_management.domain import KIND_SPECS, PROPOSAL_KIND_VALUES


def test_kind_specs_cover_exactly_proposal_kind_values() -> None:
    assert tuple(KIND_SPECS) == PROPOSAL_KIND_VALUES
    assert all(spec.name == name for name, spec in KIND_SPECS.items())


def test_representative_specs_match_current_behavior() -> None:
    assert KIND_SPECS["reference"].auto_approve_on_intake is True
    assert KIND_SPECS["event"].conflict_participant is True
    assert KIND_SPECS["routine"].conflict_participant is True
    assert KIND_SPECS["task"].auto_approve_on_intake is False

    assert KIND_SPECS["event"].schedulable is True
    assert all(
        KIND_SPECS[name].schedulable is False
        for name in PROPOSAL_KIND_VALUES
        if name != "event"
    )

    assert {name for name, spec in KIND_SPECS.items() if spec.duplicate_merge_participant} == {"task", "event"}

    assert KIND_SPECS["reference"].export_item_type == "reference"
    assert KIND_SPECS["reference"].export_disposition == "reference"
    assert KIND_SPECS["question"].export_item_type == "task"
    assert KIND_SPECS["decision"].export_item_type == "task"

    assert KIND_SPECS["event"].reminder_message_type == "event_reminder"
    assert KIND_SPECS["routine"].reminder_message_type == "routine_reminder"
