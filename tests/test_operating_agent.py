from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from task_management.domain import IncomingMessage
from task_management.operating_agent import (
    OPERATING_AGENT_SCHEMA,
    OPERATING_DECISION_OUTPUT_SCHEMA,
    RuleBasedTeamTaskOperatingAgent,
    validate_operating_decision_payload,
)
from task_management.simulator import TeamTaskSimulator


NOW = datetime(2026, 5, 5, 10, 0, 0)
BABYFAIR_MESSAGE = "\uc6cc\ud06c\uc20d \uc774\ubc88\uc8fc \ubaa9~\uc77c \uc911 \ud558\ub8e8 \uac00\uc57c\ud568"
BABYFAIR_FEEDBACK = "\ud1a0\uc694\uc77c \uc624\uc804\uc5d0 \ub098\ub791 \ud300\uc6d0 \uac19\uc774 \uac08\uac8c"


def _message(text: str, message_id: str = "dm/me/agent") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        sender_id="me",
        chat_id="me",
        visibility="private",
        text=text,
        received_at=NOW,
    )


def test_rule_based_operating_agent_emits_strict_proposal_draft_schema() -> None:
    agent = RuleBasedTeamTaskOperatingAgent()

    decision = agent.decide(
        _message(BABYFAIR_MESSAGE),
        pending_approval_requests=(),
        pending_proposals=(),
    )
    payload = decision.to_payload()

    assert OPERATING_DECISION_OUTPUT_SCHEMA["additionalProperties"] is False
    assert payload["schema"] == OPERATING_AGENT_SCHEMA
    assert payload["action"] == "create_proposals"
    assert payload["source"] == "rule_based"
    assert len(payload["proposal_drafts"]) == 1
    draft = payload["proposal_drafts"][0]
    assert draft["assigned_to"] == "shared"
    assert draft["item_type"] == "event"
    assert draft["metadata"]["date_window_start"] == "2026-05-07"
    assert draft["metadata"]["date_window_end"] == "2026-05-10"

    with pytest.raises(ValueError, match="extra"):
        validate_operating_decision_payload({**payload, "unexpected": True})


def test_operating_decision_validation_rejects_unsupported_draft_enums() -> None:
    agent = RuleBasedTeamTaskOperatingAgent()
    payload = agent.decide(
        _message(BABYFAIR_MESSAGE),
        pending_approval_requests=(),
        pending_proposals=(),
    ).to_payload()

    unsupported_type = deepcopy(payload)
    unsupported_type["proposal_drafts"][0]["item_type"] = "memo"
    with pytest.raises(ValueError, match="proposal_drafts\\[\\]\\.item_type"):
        validate_operating_decision_payload(unsupported_type)

    unsupported_assignee = deepcopy(payload)
    unsupported_assignee["proposal_drafts"][0]["assigned_to"] = "vendor"
    with pytest.raises(ValueError, match="proposal_drafts\\[\\]\\.assigned_to"):
        validate_operating_decision_payload(unsupported_assignee)


def test_orchestrator_records_agent_decision_then_core_enforces_missing_slots(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)

    result = sim.send_private(
        "me",
        BABYFAIR_MESSAGE,
        message_id="dm/me/agent-core",
        received_at=NOW,
    )

    events = sim.store.read_events()
    assert [event["type"] for event in events[:3]] == [
        "message.received",
        "agent.decision.created",
        "proposal.created",
    ]
    assert events[1]["payload"]["decision"]["action"] == "create_proposals"
    assert result.proposals[0].kind == "question"
    assert result.proposals[0].missing_slots == ("exact_date", "participants")
    assert result.approval_requests[0].approver_id == "me"
    assert "*정확한 날짜*, *참여자*" in result.outbound_messages[0].text
    assert "담당자는 팀 공동" in result.outbound_messages[0].text
    assert f"`변경 {result.approval_requests[0].request_id} ..." in result.outbound_messages[0].text


def test_operating_agent_feedback_patch_is_applied_by_core_policy(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    sim.send_private(
        "me",
        BABYFAIR_MESSAGE,
        message_id="dm/me/agent-feedback-source",
        received_at=NOW,
    )

    resolved = sim.send_private(
        "me",
        BABYFAIR_FEEDBACK,
        message_id="dm/me/agent-feedback-reply",
        received_at=NOW.replace(hour=10, minute=5),
    )

    decisions = [event for event in sim.store.read_events() if event["type"] == "agent.decision.created"]
    assert decisions[-1]["payload"]["decision"]["action"] == "apply_feedback"
    patch = decisions[-1]["payload"]["decision"]["proposal_patches"][0]
    assert patch["reason"] == "resolve_pending_question"
    assert patch["temporal_update"]["scheduled_date"] == "2026-05-09"
    assert resolved.proposals[0].status == "approved"
    assert resolved.proposals[0].scheduled_date.isoformat() == "2026-05-09"
