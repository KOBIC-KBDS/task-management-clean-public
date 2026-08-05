from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from task_management.codex_operating_agent import (
    CODEX_DECISION_OUTPUT_SCHEMA,
    CodexCliOperatingAgent,
    CodexCliOperatingAgentConfig,
)
from task_management.domain import ApprovalRequest, IncomingMessage, Proposal
from task_management.operating_agent import (
    OPERATING_DECISION_OUTPUT_SCHEMA,
    DirectResponse,
    OperatingAgentDecision,
    ProposalPatch,
    RuleBasedTeamTaskOperatingAgent,
    decision_from_payload,
    decision_to_payload,
)
from task_management.operating_agent_prompt import build_operating_agent_system_instructions
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.store import TeamTaskStore


NOW = datetime(2026, 8, 5, 10, 0, 0)


class StaticDecisionAgent:
    def __init__(self, decision: OperatingAgentDecision) -> None:
        self.decision = decision

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        return self.decision


class FailingCodexRunner:
    def run_decision(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any],
        config: CodexCliOperatingAgentConfig,
    ) -> str:
        raise RuntimeError("codex unavailable")


def test_direct_response_round_trips_through_strict_decision_schema() -> None:
    decision = OperatingAgentDecision(
        action="respond",
        source="semantic_test",
        confidence=0.98,
        rationale="The user asked for a read-only explanation.",
        direct_responses=(
            DirectResponse(
                recipient_id="me",
                text="이 항목은 외부 기관에 교육 내용의 상세 설명을 요청하는 메일 발송 작업입니다.",
                response_type="explanation",
                proposal_id="proposal/mail",
                request_id="approval/mail",
                interaction_label="request",
                evidence_text="상세하게 설명해줘",
                confidence=0.97,
            ),
        ),
    )

    payload = decision_to_payload(decision)
    restored = decision_from_payload(payload)

    assert restored == decision
    assert "direct_responses" in OPERATING_DECISION_OUTPUT_SCHEMA["required"]
    assert "direct_responses" in CODEX_DECISION_OUTPUT_SCHEMA["required"]
    assert "respond" in OPERATING_DECISION_OUTPUT_SCHEMA["properties"]["action"]["enum"]


def test_respond_action_requires_a_direct_response() -> None:
    with pytest.raises(ValueError, match="respond requires"):
        decision_to_payload(OperatingAgentDecision(action="respond"))


def test_explanation_response_preserves_pending_state_and_marks_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal, request = _pending_mail()
    _seed(store, proposal, request)
    response = DirectResponse(
        recipient_id="me",
        text=(
            "이 항목은 인실리코젠에 교육 커리큘럼과 교육 내용의 상세 설명을 요청하는 외부 메일 발송 작업입니다. "
            "현재 확인은 메일 발송 작업을 진행 대상으로 승인할지 묻는 것이며, 워크플로 단계를 더 만들라는 뜻은 아닙니다."
        ),
        response_type="explanation",
        proposal_id=proposal.proposal_id,
        request_id=request.request_id,
        interaction_label="request",
        evidence_text="이거에 대해서 뭘 해야하는지 상세하게 설명해줘",
        confidence=0.99,
    )
    orchestrator = TeamTaskOrchestrator(
        store,
        operating_agent=StaticDecisionAgent(
            OperatingAgentDecision(
                action="respond",
                source="semantic_test",
                confidence=0.99,
                rationale="Existing-item explanation is read-only.",
                direct_responses=(response,),
            )
        ),
    )

    result = orchestrator.handle_message(_message("[요청] 이거에 대해서 뭘 해야하는지 상세하게 설명해줘"))

    assert result.proposals == ()
    assert result.approval_requests == ()
    assert len(result.outbound_messages) == 1
    outbound = result.outbound_messages[0]
    assert outbound.message_type == "agent_direct_response"
    assert outbound.text.startswith("*[요청]*\n")
    assert outbound.proposal_id == proposal.proposal_id
    assert outbound.approval_request_id == request.request_id
    assert store.get_proposal(proposal.proposal_id) == proposal
    assert store.get_approval_request(request.request_id) == request
    assert "agent.direct_response.accepted" in [event["type"] for event in store.read_events()]


def test_general_task_management_conversation_needs_no_tag_or_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    decision = OperatingAgentDecision(
        action="respond",
        source="semantic_test",
        confidence=0.98,
        rationale="The user is discussing how to organize work without requesting a mutation.",
        direct_responses=(
            DirectResponse(
                recipient_id="me",
                text="네. 먼저 현재 구조를 설명하고, 원하시면 그다음 메시지에서 실제 변경으로 이어갈 수 있습니다.",
                response_type="answer",
                interaction_label="",
                evidence_text="그냥 대화 형식은 안되는건가",
                confidence=0.98,
            ),
        ),
    )

    result = TeamTaskOrchestrator(store, operating_agent=StaticDecisionAgent(decision)).handle_message(
        _message("그냥 대화 형식은 안되는건가")
    )

    assert result.proposals == ()
    assert result.approval_requests == ()
    assert result.outbound_messages[0].message_type == "agent_direct_response"
    assert result.outbound_messages[0].text.startswith("네.")
    assert "[요청]" not in result.outbound_messages[0].text


def test_tagged_private_request_cannot_disappear_as_no_action(tmp_path: Path) -> None:
    store = _store(tmp_path)
    decision = OperatingAgentDecision(
        action="no_action",
        source="semantic_test",
        confidence=0.2,
        rationale="The backend failed to produce a structured result.",
    )

    result = TeamTaskOrchestrator(store, operating_agent=StaticDecisionAgent(decision)).handle_message(
        _message("[요청] 기존 작업을 정리해줘")
    )

    assert result.proposals == ()
    assert result.outbound_messages[0].message_type == "explicit_request_unhandled"
    assert result.outbound_messages[0].card["reason"] == "tagged_request_returned_no_action"
    assert "임의로 바꾸지는 않았습니다" in result.outbound_messages[0].text
    assert "agent.explicit_request.unhandled" in [event["type"] for event in store.read_events()]


def test_direct_response_survives_a_same_turn_feedback_patch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal, request = _pending_mail()
    _seed(store, proposal, request)
    changed_title = "인실리코젠 교육 상세설명 요청메일 발송"
    decision = OperatingAgentDecision(
        action="apply_feedback",
        source="semantic_test",
        confidence=0.99,
        rationale="The same message asks for an explanation and corrects the title.",
        proposal_patches=(
            ProposalPatch(
                request_id="",
                proposal_id=proposal.proposal_id,
                actor_id="me",
                body="작업 의미를 설명하고 제목도 명확하게 바꿔줘",
                temporal_update={"title": changed_title, "semantic_update_type": "correction"},
                reason="mixed_explanation_and_correction",
                target_confidence=0.99,
                evidence_text=proposal.title,
            ),
        ),
        direct_responses=(
            DirectResponse(
                recipient_id="me",
                text="외부 기관에 교육 상세설명을 요청하는 메일 발송 작업입니다. 제목도 더 명확하게 고칩니다.",
                response_type="explanation",
                proposal_id=proposal.proposal_id,
                request_id=request.request_id,
                interaction_label="request",
                evidence_text=proposal.title,
                confidence=0.99,
            ),
        ),
    )

    result = TeamTaskOrchestrator(store, operating_agent=StaticDecisionAgent(decision)).handle_message(
        _message("[요청] 의미를 설명하고 제목도 고쳐줘")
    )

    assert "agent_direct_response" in {message.message_type for message in result.outbound_messages}
    assert len(result.outbound_messages) >= 2
    changed = store.get_proposal(proposal.proposal_id)
    assert changed is not None
    assert changed.title == changed_title


def test_direct_response_cannot_target_another_recipient(tmp_path: Path) -> None:
    store = _store(tmp_path)
    decision = OperatingAgentDecision(
        action="respond",
        source="semantic_test",
        direct_responses=(
            DirectResponse(
                recipient_id="someone-else",
                text="Private answer",
                interaction_label="request",
            ),
        ),
    )

    result = TeamTaskOrchestrator(store, operating_agent=StaticDecisionAgent(decision)).handle_message(
        _message("Explain this")
    )

    assert result.outbound_messages == ()
    rejected = [event for event in store.read_events() if event["type"] == "agent.direct_response.rejected"]
    assert rejected[-1]["payload"]["reason"] == "recipient_mismatch"


def test_shared_prompt_routes_explanations_to_direct_responses() -> None:
    prompt = build_operating_agent_system_instructions(
        backend_runtime="test runtime",
        source_key_prefix="test",
    )

    assert "use direct_responses" in prompt
    assert "Do not encode an explanation as a proposal_patch" in prompt
    assert "`[요청]` is optional, but when present it is an explicit instruction" in prompt
    assert "Never return no_action for a private `[요청]` message" in prompt
    assert "Do not downgrade an imperative mutation request to a read-only response" in prompt
    assert "Never copy [요청] into a task title" in prompt


def test_rule_fallback_explains_recent_pending_item_without_creating_work() -> None:
    proposal, request = _pending_mail()
    proposal = replace(
        proposal,
        metadata={**proposal.metadata, "parent_proposal_id": "proposal/study", "workflow_title": "제3회 ai-study"},
    )
    message = IncomingMessage(
        message_id="dm/me/fallback-explanation",
        sender_id="me",
        chat_id="dm/me",
        visibility="private",
        text="[요청] 이거에 대해서 뭘 해야하는지 상세하게 설명해줘",
        received_at=NOW,
        recent_conversation=(
            {
                "role": "assistant",
                "text": "워크플로 단계 확인이 필요합니다: 인실리코젠 교육 내용 요청메일 발송",
            },
        ),
    )

    decision = RuleBasedTeamTaskOperatingAgent().decide(
        message,
        pending_approval_requests=(request,),
        pending_proposals=(proposal,),
    )

    assert decision.action == "respond"
    assert decision.proposal_drafts == ()
    assert decision.proposal_patches == ()
    assert decision.direct_responses[0].interaction_label == "request"
    assert "워크플로 단계를 더 만들라는 뜻이 아니라" in decision.direct_responses[0].text
    assert "자동으로 발송하지 않습니다" in decision.direct_responses[0].text


def test_codex_failure_preserves_read_only_explanation_fallback() -> None:
    proposal, request = _pending_mail()
    message = IncomingMessage(
        message_id="dm/me/codex-fallback-explanation",
        sender_id="me",
        chat_id="dm/me",
        visibility="private",
        text="이 항목이 무슨 뜻인지 설명해줘",
        received_at=NOW,
        recent_conversation=(
            {"role": "assistant", "text": "확인이 필요합니다: 인실리코젠 교육 내용 요청메일 발송"},
        ),
    )
    agent = CodexCliOperatingAgent(
        CodexCliOperatingAgentConfig(fallback_on_error=True),
        runner=FailingCodexRunner(),
    )

    decision = agent.decide(
        message,
        pending_approval_requests=(request,),
        pending_proposals=(proposal,),
    )

    assert decision.source == "codex_cli_fallback"
    assert decision.action == "respond"
    assert decision.direct_responses[0].proposal_id == proposal.proposal_id


def _store(tmp_path: Path) -> TeamTaskStore:
    return TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")


def _seed(store: TeamTaskStore, proposal: Proposal, request: ApprovalRequest) -> None:
    store.save_proposal(proposal)
    store.save_approval_request(request)


def _pending_mail() -> tuple[Proposal, ApprovalRequest]:
    proposal = Proposal(
        proposal_id="proposal/mail",
        source_message_id="dm/me/mail",
        proposer_id="me",
        title="인실리코젠 교육 내용 요청메일 발송",
        raw_text="교육 내용에 대한 상세설명을 요청하는 메일을 보낸다.",
        kind="task",
        status="awaiting_approval",
        assigned_to="me",
        task_management_area="work",
        discussion_id="private/dm/me",
        message_id="dm/me/mail/1",
        required_approvers=("me",),
        created_at=NOW,
        updated_at=NOW,
        metadata={
            "external_owner": "인실리코젠",
            "materials": "교육 커리큘럼, 교육 내용",
            "requires_separate_approval": "true",
            "risk_reason": "external_email_sending",
        },
    )
    request = ApprovalRequest(
        request_id="approval/mail",
        proposal_id=proposal.proposal_id,
        approver_id="me",
        requested_at=NOW,
    )
    return proposal, request


def _message(text: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"dm/me/{abs(hash(text))}",
        sender_id="me",
        chat_id="dm/me",
        visibility="private",
        text=text,
        received_at=NOW,
    )
