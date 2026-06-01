from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from task_management.domain import IncomingMessage
from task_management.openai_operating_agent import (
    OpenAIOperatingAgentConfig,
    OpenAIOperatingAgentError,
    OpenAIResponsesOperatingAgent,
)
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 5, 10, 0, 0)
BABYFAIR_MESSAGE = "\uc6cc\ud06c\uc20d \uc774\ubc88\uc8fc \ubaa9~\uc77c \uc911 \ud558\ub8e8 \uac00\uc57c\ud568"
AI_TITLE = "AI \uc6cc\ud06c\uc20d \ubc29\ubb38"


class EchoBaselineClient:
    def __init__(self) -> None:
        self.requests: list[Mapping[str, Any]] = []

    def create_response(self, payload: Mapping[str, Any], *, timeout_seconds: float) -> Mapping[str, Any]:
        self.requests.append(payload)
        context = json.loads(str(payload["input"]))
        decision = context["deterministic_baseline"]
        decision["source"] = "model"
        decision["rationale"] = "LLM normalized task_management wording."
        decision["proposal_drafts"][0]["title"] = AI_TITLE
        return {"output_text": json.dumps(decision, ensure_ascii=False)}


class BrokenClient:
    def create_response(self, payload: Mapping[str, Any], *, timeout_seconds: float) -> Mapping[str, Any]:
        raise RuntimeError("network unavailable")


def _message() -> IncomingMessage:
    return IncomingMessage(
        message_id="dm/me/openai",
        sender_id="me",
        chat_id="dm/me",
        visibility="private",
        text=BABYFAIR_MESSAGE,
        received_at=NOW,
    )


def test_openai_operating_agent_requests_structured_output_and_decodes_decision() -> None:
    client = EchoBaselineClient()
    agent = OpenAIResponsesOperatingAgent(
        OpenAIOperatingAgentConfig(api_key="test", model="gpt-test"),
        client=client,
    )

    decision = agent.decide(_message(), pending_approval_requests=(), pending_proposals=())

    assert decision.source == "openai_responses"
    assert decision.proposal_drafts[0].title == AI_TITLE
    assert "model=gpt-test" in decision.rationale
    request = client.requests[0]
    assert request["model"] == "gpt-test"
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    assert "do not invent a new item_type" in request["instructions"]
    assert "type_policy_needed=true" in request["instructions"]
    assert "depends_on_proposal_ids" in request["instructions"]
    assert "step_index" in request["instructions"]
    assert "workflow_id" in request["instructions"]
    context = json.loads(str(request["input"]))
    assert context["rules"]["state_owner"].startswith("TeamTaskOrchestrator")


def test_orchestrator_commits_openai_agent_draft_through_core_policy(tmp_path: Path) -> None:
    store = TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")
    agent = OpenAIResponsesOperatingAgent(
        OpenAIOperatingAgentConfig(api_key="test", model="gpt-test"),
        client=EchoBaselineClient(),
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(_message())

    proposal = result.proposals[0]
    assert proposal.title == AI_TITLE
    assert proposal.kind == "question"
    assert proposal.status == "awaiting_approval"
    assert proposal.missing_slots == ("exact_date", "participants")
    decision_event = next(event for event in store.read_events() if event["type"] == "agent.decision.created")
    assert decision_event["payload"]["decision"]["source"] == "openai_responses"


def test_openai_operating_agent_falls_back_to_rule_decision_on_error() -> None:
    agent = OpenAIResponsesOperatingAgent(
        OpenAIOperatingAgentConfig(api_key="test", fallback_on_error=True),
        client=BrokenClient(),
    )

    decision = agent.decide(_message(), pending_approval_requests=(), pending_proposals=())

    assert decision.source == "rule_based_fallback"
    assert decision.action == "create_proposals"
    assert "network unavailable" in decision.rationale


def test_openai_operating_agent_can_fail_closed_when_fallback_is_disabled() -> None:
    agent = OpenAIResponsesOperatingAgent(
        OpenAIOperatingAgentConfig(api_key="test", fallback_on_error=False),
        client=BrokenClient(),
    )

    with pytest.raises(OpenAIOperatingAgentError, match="network unavailable"):
        agent.decide(_message(), pending_approval_requests=(), pending_proposals=())
