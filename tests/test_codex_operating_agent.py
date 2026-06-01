from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from task_management.codex_operating_agent import (
    CodexCliOperatingAgent,
    CodexCliOperatingAgentConfig,
    CodexOperatingAgentError,
)
from task_management.domain import ApprovalRequest, IncomingMessage, Proposal
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 5, 10, 0, 0)
BABYFAIR_MESSAGE = "\uc6cc\ud06c\uc20d \uc774\ubc88\uc8fc \ubaa9~\uc77c \uc911 \ud558\ub8e8 \uac00\uc57c\ud568"
CODEX_TITLE = "Codex \uc6cc\ud06c\uc20d \ucc38\uc11d"
COMMITTEE_MESSAGE = (
    "\ub0b4\uc77c \uc624\ud6c4 4\uc2dc \ubc18 \ub9ac\ubdf0\uc704\uc6d0\ud68c \ubc1c\ud45c \ubc30\uc11d. "
    "\ucc38\uc11d\uc790 \ub098/\uae40\uc13c\ud130 \uc13c\ud130\uc7a5\ub2d8/\uc774\ud611\uc5c5 \uc120\uc0dd\ub2d8"
)


class RecordingCodexRunner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.prompts: list[str] = []
        self.schemas: list[Mapping[str, Any]] = []
        self.configs: list[CodexCliOperatingAgentConfig] = []

    def run_decision(self, prompt: str, *, schema: Mapping[str, Any], config: CodexCliOperatingAgentConfig) -> str:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        self.configs.append(config)
        if self.fail:
            raise RuntimeError("codex unavailable")
        start = prompt.index("<task_management_context>") + len("<task_management_context>")
        end = prompt.index("</task_management_context>")
        context = json.loads(prompt[start:end])
        decision = context["deterministic_baseline"]
        decision["source"] = "model"
        decision["rationale"] = "Codex normalized task_management wording."
        decision["proposal_drafts"][0]["title"] = CODEX_TITLE
        return json.dumps(decision, ensure_ascii=False)


class MetadataJsonCodexRunner:
    def run_decision(self, prompt: str, *, schema: Mapping[str, Any], config: CodexCliOperatingAgentConfig) -> str:
        assert "metadata_json" in json.dumps(schema)
        return json.dumps(
            {
                "schema": "task-task_management.operating-agent.v1",
                "action": "create_proposals",
                "source": "model",
                "confidence": 0.98,
                "rationale": "Semantic Codex parse.",
                "proposal_drafts": [
                    {
                        "source_key": "codex/dm/me/committee/1",
                        "raw_text": COMMITTEE_MESSAGE,
                        "title": "\ub9ac\ubdf0\uc704\uc6d0\ud68c \ubc1c\ud45c \ubc30\uc11d",
                        "discussion_id": "private/dm/me/dm/me/committee",
                        "message_id": "dm/me/committee/1",
                        "line_number": 1,
                        "speaker": "me",
                        "assigned_to": "me",
                        "task_management_area": "general",
                        "due_date": "",
                        "scheduled_date": "2026-05-06",
                        "time_window": "16:30",
                        "task_status": "active",
                        "item_type": "event",
                        "disposition": "execution",
                        "needs_review": False,
                        "source_url": "",
                        "source_export_path": "",
                        "metadata_json": json.dumps(
                            {
                                "attendees": "\ub098/\uae40\uc13c\ud130 \uc13c\ud130\uc7a5\ub2d8/\uc774\ud611\uc5c5 \uc120\uc0dd\ub2d8"
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
                "proposal_patches": [],
                "clarification_questions": [],
            },
            ensure_ascii=False,
        )


class PatchJsonCodexRunner:
    def run_decision(self, prompt: str, *, schema: Mapping[str, Any], config: CodexCliOperatingAgentConfig) -> str:
        context = json.loads(prompt[prompt.index("<task_management_context>") + len("<task_management_context>") : prompt.index("</task_management_context>")])
        request = context["pending_approval_requests"][0]
        proposal = context["pending_proposals"][0]
        return json.dumps(
            {
                "schema": "task-task_management.operating-agent.v1",
                "action": "apply_feedback",
                "source": "model",
                "confidence": 1.0,
                "rationale": "Semantic feedback patch.",
                "proposal_drafts": [],
                "proposal_patches": [
                    {
                        "request_id": request["request_id"],
                        "proposal_id": proposal["proposal_id"],
                        "actor_id": "me",
                        "body": "\ud1a0\uc694\uc77c \uc624\ud6c4 4\uc2dc \ubc18 \ub098\ub791 \uae40\uc13c\ud130 \uc13c\ud130\uc7a5\ub2d8",
                        "temporal_update_json": json.dumps(
                            {
                                "scheduled_date": "2026-05-09",
                                "time_window": "16:30",
                                "participants": "me",
                                "external_participants": "\uae40\uc13c\ud130 \uc13c\ud130\uc7a5\ub2d8",
                                "participant_label": "\ub098/\uae40\uc13c\ud130 \uc13c\ud130\uc7a5\ub2d8",
                                "location_optional": "true",
                            },
                            ensure_ascii=False,
                        ),
                        "reason": "resolve_pending_question",
                    }
                ],
                "clarification_questions": [],
            },
            ensure_ascii=False,
        )


def _message() -> IncomingMessage:
    return IncomingMessage(
        message_id="dm/me/codex",
        sender_id="me",
        chat_id="dm/me",
        visibility="private",
        text=BABYFAIR_MESSAGE,
        received_at=NOW,
    )


def test_codex_cli_operating_agent_uses_login_session_contract_without_api_key() -> None:
    runner = RecordingCodexRunner()
    agent = CodexCliOperatingAgent(
        CodexCliOperatingAgentConfig(model="gpt-test", strip_api_key_env=True),
        runner=runner,
    )

    decision = agent.decide(_message(), pending_approval_requests=(), pending_proposals=())

    assert decision.source == "codex_cli"
    assert decision.proposal_drafts[0].title == CODEX_TITLE
    assert "codex_cli_login_session" in decision.rationale
    assert runner.schemas[0]["additionalProperties"] is False
    assert runner.configs[0].strip_api_key_env is True
    assert "Never request, print, or depend on an API key." in runner.prompts[0]
    assert '"auth_policy": "Use the local Codex login session only; do not ask for or output API keys."' in runner.prompts[0]
    assert "do not invent a new item_type" in runner.prompts[0]
    assert "type_policy_needed=true" in runner.prompts[0]
    assert "depends_on_proposal_ids" in runner.prompts[0]
    assert "step_index" in runner.prompts[0]
    assert "workflow_id" in runner.prompts[0]


def test_orchestrator_commits_codex_agent_draft_through_core_policy(tmp_path: Path) -> None:
    store = TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")
    agent = CodexCliOperatingAgent(
        CodexCliOperatingAgentConfig(model="gpt-test"),
        runner=RecordingCodexRunner(),
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(_message())

    proposal = result.proposals[0]
    assert proposal.title == CODEX_TITLE
    assert proposal.kind == "question"
    assert proposal.status == "awaiting_approval"
    assert proposal.missing_slots == ("exact_date", "participants")
    decision_event = next(event for event in store.read_events() if event["type"] == "agent.decision.created")
    assert decision_event["payload"]["decision"]["source"] == "codex_cli"


def test_codex_metadata_json_normalizes_external_attendees_and_optional_location(tmp_path: Path) -> None:
    store = TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")
    agent = CodexCliOperatingAgent(CodexCliOperatingAgentConfig(model="gpt-test"), runner=MetadataJsonCodexRunner())
    message = IncomingMessage(
        message_id="dm/me/committee",
        sender_id="me",
        chat_id="dm/me",
        visibility="private",
        text=COMMITTEE_MESSAGE,
        received_at=NOW,
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(message)

    proposal = result.proposals[0]
    assert proposal.status == "approved"
    assert proposal.kind == "event"
    assert proposal.time_window == "16:30"
    assert proposal.metadata["participants"] == "me"
    assert proposal.metadata["external_participants"] == "나/김센터 센터장님/이협업 선생님"
    assert proposal.metadata["location_optional"] == "true"


def test_codex_patch_temporal_update_is_applied_without_rule_reparse(tmp_path: Path) -> None:
    store = TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")
    pending = Proposal(
        proposal_id="proposal/pending",
        source_message_id="dm/me/pending",
        proposer_id="me",
        title="Example Lab 워크숍 방문",
        raw_text="Example Lab 워크숍 이번주 목~일 중 하루 가야함",
        kind="question",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="childcare",
        discussion_id="private/dm/me/pending",
        message_id="dm/me/pending/1",
        required_approvers=("me",),
        missing_slots=("exact_date", "participants"),
        created_at=NOW,
        updated_at=NOW,
    )
    request = ApprovalRequest(
        request_id="approval/pending",
        proposal_id=pending.proposal_id,
        approver_id="me",
        requested_at=NOW,
    )
    store.save_proposal(pending)
    store.save_approval_request(request)
    agent = CodexCliOperatingAgent(CodexCliOperatingAgentConfig(model="gpt-test"), runner=PatchJsonCodexRunner())
    message = IncomingMessage(
        message_id="dm/me/patch",
        sender_id="me",
        chat_id="dm/me",
        visibility="private",
        text="\ud1a0\uc694\uc77c \uc624\ud6c4 4\uc2dc \ubc18 \ub098\ub791 \uae40\uc13c\ud130 \uc13c\ud130\uc7a5\ub2d8",
        received_at=NOW,
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(message)

    updated = result.proposals[0]
    assert updated.status == "approved"
    assert updated.scheduled_date.isoformat() == "2026-05-09"
    assert updated.time_window == "16:30"
    assert updated.metadata["participant_label"] == "나/김센터 센터장님"


def test_codex_cli_operating_agent_falls_back_to_rule_decision_on_error() -> None:
    agent = CodexCliOperatingAgent(
        CodexCliOperatingAgentConfig(fallback_on_error=True),
        runner=RecordingCodexRunner(fail=True),
    )

    decision = agent.decide(_message(), pending_approval_requests=(), pending_proposals=())

    assert decision.source == "codex_cli_fallback"
    assert decision.action == "create_proposals"
    assert "codex unavailable" in decision.rationale


def test_orchestrator_logs_codex_fallback_as_runtime_observability(tmp_path: Path) -> None:
    store = TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")
    agent = CodexCliOperatingAgent(
        CodexCliOperatingAgentConfig(fallback_on_error=True),
        runner=RecordingCodexRunner(fail=True),
    )

    TeamTaskOrchestrator(store, operating_agent=agent).handle_message(_message())

    fallback_event = next(event for event in store.read_events() if event["type"] == "agent.fallback.used")
    assert fallback_event["payload"]["source"] == "codex_cli_fallback"
    assert fallback_event["payload"]["message_id"] == "dm/me/codex"


def test_codex_cli_operating_agent_can_fail_closed_when_fallback_is_disabled() -> None:
    agent = CodexCliOperatingAgent(
        CodexCliOperatingAgentConfig(fallback_on_error=False),
        runner=RecordingCodexRunner(fail=True),
    )

    with pytest.raises(CodexOperatingAgentError, match="codex unavailable"):
        agent.decide(_message(), pending_approval_requests=(), pending_proposals=())
