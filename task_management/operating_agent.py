from __future__ import annotations

from .relations import (
    ATTENDEES_KEY,
    DATE_WINDOW_START_KEY,
    EXTERNAL_PARTICIPANTS_KEY,
    LOCATION_KEY,
    LOCATION_OPTIONAL_KEY,
    PARTICIPANTS_KEY,
    PARTICIPANT_LABEL_KEY,
    PROGRESS_PERCENT_KEY,
    PROGRESS_STATUS_KEY,
    REMAINING_WORK_KEY,
)

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Any, Literal, Mapping, Protocol, Sequence

from .discussion_adapter import parse_manual_discussion, parse_temporal_update
from .domain import (
    ASSIGNEE_VALUES,
    PROPOSAL_KIND_VALUES,
    ApprovalRequest,
    TeamTaskTaskCandidate,
    IncomingMessage,
    Proposal,
)
from .feedback_scoring import MIN_RUNNER_UP_GAP, MIN_TARGET_SCORE, pick_best_candidate


OPERATING_AGENT_SCHEMA = "task-task_management.operating-agent.v1"
OperatingAction = Literal["create_proposals", "apply_feedback", "no_action"]

PROPOSAL_DRAFT_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "source_key",
        "raw_text",
        "title",
        "discussion_id",
        "message_id",
        "line_number",
        "speaker",
        "assigned_to",
        "task_management_area",
        "due_date",
        "scheduled_date",
        "time_window",
        "task_status",
        "item_type",
        "disposition",
        "needs_review",
        "source_url",
        "source_export_path",
        "metadata",
    ],
    "properties": {
        "source_key": {"type": "string"},
        "raw_text": {"type": "string"},
        "title": {"type": "string"},
        "discussion_id": {"type": "string"},
        "message_id": {"type": "string"},
        "line_number": {"type": "integer"},
        "speaker": {"type": "string"},
        "assigned_to": {"type": "string", "enum": list(ASSIGNEE_VALUES)},
        "task_management_area": {"type": "string"},
        "due_date": {"type": "string"},
        "scheduled_date": {"type": "string"},
        "time_window": {"type": "string"},
        "task_status": {"type": "string"},
        "item_type": {"type": "string", "enum": list(PROPOSAL_KIND_VALUES)},
        "disposition": {"type": "string"},
        "needs_review": {"type": "boolean"},
        "source_url": {"type": "string"},
        "source_export_path": {"type": "string"},
        "metadata": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
}

PROPOSAL_PATCH_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "request_id",
        "proposal_id",
        "actor_id",
        "body",
        "temporal_update",
        "reason",
        "target_confidence",
        "evidence_text",
        "assumptions",
        "missing_slots",
        "needs_clarification",
    ],
    "properties": {
        "request_id": {"type": "string"},
        "proposal_id": {"type": "string"},
        "actor_id": {"type": "string"},
        "body": {"type": "string"},
        "temporal_update": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "reason": {"type": "string"},
        "target_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_text": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "missing_slots": {"type": "array", "items": {"type": "string"}},
        "needs_clarification": {"type": "boolean"},
    },
}

CLARIFICATION_QUESTION_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["recipient_id", "prompt", "proposal_id", "missing_slots"],
    "properties": {
        "recipient_id": {"type": "string"},
        "prompt": {"type": "string"},
        "proposal_id": {"type": "string"},
        "missing_slots": {"type": "array", "items": {"type": "string"}},
    },
}


OPERATING_DECISION_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "action",
        "source",
        "confidence",
        "rationale",
        "proposal_drafts",
        "proposal_patches",
        "clarification_questions",
    ],
    "properties": {
        "schema": {"type": "string", "const": OPERATING_AGENT_SCHEMA},
        "action": {"type": "string", "enum": ["create_proposals", "apply_feedback", "no_action"]},
        "source": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
        "proposal_drafts": {"type": "array", "items": PROPOSAL_DRAFT_OUTPUT_SCHEMA},
        "proposal_patches": {"type": "array", "items": PROPOSAL_PATCH_OUTPUT_SCHEMA},
        "clarification_questions": {"type": "array", "items": CLARIFICATION_QUESTION_OUTPUT_SCHEMA},
    },
}


@dataclass(frozen=True)
class ProposalDraft:
    """Agent output shape for a proposal candidate before core policy is applied."""

    source_key: str
    raw_text: str
    title: str
    discussion_id: str
    message_id: str
    line_number: int
    speaker: str = ""
    assigned_to: str = "unassigned"
    task_management_area: str = "general"
    due_date: date | None = None
    scheduled_date: date | None = None
    time_window: str = ""
    task_status: str = "active"
    item_type: str = "task"
    disposition: str = "execution"
    needs_review: bool = False
    source_url: str = ""
    source_export_path: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_candidate(cls, candidate: TeamTaskTaskCandidate) -> "ProposalDraft":
        return cls(
            source_key=candidate.source_key,
            raw_text=candidate.raw_text,
            title=candidate.title,
            discussion_id=candidate.discussion_id,
            message_id=candidate.message_id,
            line_number=candidate.line_number,
            speaker=candidate.speaker,
            assigned_to=candidate.assigned_to,
            task_management_area=candidate.task_management_area,
            due_date=candidate.due_date,
            scheduled_date=candidate.scheduled_date,
            time_window=candidate.time_window,
            task_status=candidate.task_status,
            item_type=candidate.item_type,
            disposition=candidate.disposition,
            needs_review=candidate.needs_review,
            source_url=candidate.source_url,
            source_export_path=candidate.source_export_path,
            metadata=dict(candidate.metadata),
        )

    def to_candidate(self) -> TeamTaskTaskCandidate:
        return TeamTaskTaskCandidate(
            source_key=self.source_key,
            raw_text=self.raw_text,
            title=self.title,
            discussion_id=self.discussion_id,
            message_id=self.message_id,
            line_number=self.line_number,
            speaker=self.speaker,
            assigned_to=self.assigned_to,
            task_management_area=self.task_management_area,
            due_date=self.due_date,
            scheduled_date=self.scheduled_date,
            time_window=self.time_window,
            task_status=self.task_status,
            item_type=self.item_type,
            disposition=self.disposition,
            needs_review=self.needs_review,
            source_url=self.source_url,
            source_export_path=self.source_export_path,
            metadata=dict(self.metadata),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "source_key": self.source_key,
            "raw_text": self.raw_text,
            "title": self.title,
            "discussion_id": self.discussion_id,
            "message_id": self.message_id,
            "line_number": self.line_number,
            "speaker": self.speaker,
            "assigned_to": self.assigned_to,
            "task_management_area": self.task_management_area,
            "due_date": self.due_date.isoformat() if self.due_date else "",
            "scheduled_date": self.scheduled_date.isoformat() if self.scheduled_date else "",
            "time_window": self.time_window,
            "task_status": self.task_status,
            "item_type": self.item_type,
            "disposition": self.disposition,
            "needs_review": self.needs_review,
            "source_url": self.source_url,
            "source_export_path": self.source_export_path,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ProposalPatch:
    """Agent output shape for a constrained change to an existing proposal."""

    request_id: str
    proposal_id: str
    actor_id: str
    body: str
    temporal_update: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    target_confidence: float = 1.0
    evidence_text: str = ""
    assumptions: tuple[str, ...] = ()
    missing_slots: tuple[str, ...] = ()
    needs_clarification: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "proposal_id": self.proposal_id,
            "actor_id": self.actor_id,
            "body": self.body,
            "temporal_update": dict(self.temporal_update),
            "reason": self.reason,
            "target_confidence": self.target_confidence,
            "evidence_text": self.evidence_text,
            "assumptions": list(self.assumptions),
            "missing_slots": list(self.missing_slots),
            "needs_clarification": self.needs_clarification,
        }


@dataclass(frozen=True)
class ClarificationQuestion:
    """Agent output shape for questions that should be rendered by a channel adapter."""

    recipient_id: str
    prompt: str
    proposal_id: str = ""
    missing_slots: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "recipient_id": self.recipient_id,
            "prompt": self.prompt,
            "proposal_id": self.proposal_id,
            "missing_slots": list(self.missing_slots),
        }


@dataclass(frozen=True)
class OperatingAgentDecision:
    """Strict decision envelope: the agent proposes, the core commits or rejects."""

    action: OperatingAction
    source: str = "rule_based"
    confidence: float = 1.0
    rationale: str = ""
    proposal_drafts: tuple[ProposalDraft, ...] = ()
    proposal_patches: tuple[ProposalPatch, ...] = ()
    clarification_questions: tuple[ClarificationQuestion, ...] = ()
    schema: str = OPERATING_AGENT_SCHEMA

    def to_payload(self) -> dict[str, object]:
        return decision_to_payload(self)


class TeamTaskOperatingAgent(Protocol):
    """Boundary for LLM/agent implementations used by the task_management runtime."""

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        """Return a strict decision envelope without mutating task_management state."""


class RuleBasedTeamTaskOperatingAgent:
    """Deterministic baseline operating agent used before external LLM wiring."""

    meaningful_feedback_keys = frozenset(
        {
            "due_date",
            "scheduled_date",
            DATE_WINDOW_START_KEY,
            "time_window",
            PARTICIPANTS_KEY,
            LOCATION_KEY,
            LOCATION_OPTIONAL_KEY,
            "defer_missing_slots",
            "materials",
            "status",
            PROGRESS_STATUS_KEY,
            REMAINING_WORK_KEY,
            "semantic_update_type",
        }
    )

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        feedback = self._feedback_decision(message, pending_approval_requests, pending_proposals)
        if feedback is not None:
            return feedback
        natural_feedback = self._natural_feedback_decision(message, pending_proposals)
        if natural_feedback is not None:
            return natural_feedback

        discussion_id = f"{message.visibility}/{message.chat_id}/{message.message_id}"
        candidates = parse_manual_discussion(
            message.text,
            discussion_id=discussion_id,
            reference_date=message.received_at.date(),
        )
        drafts = tuple(ProposalDraft.from_candidate(candidate) for candidate in candidates)
        if not drafts:
            return OperatingAgentDecision(
                action="no_action",
                confidence=0.0,
                rationale="No task_management proposal candidate was detected.",
            )
        return OperatingAgentDecision(
            action="create_proposals",
            confidence=1.0,
            rationale="Parsed message into proposal drafts; core will apply approval and missing-slot policy.",
            proposal_drafts=drafts,
        )

    def _feedback_decision(
        self,
        message: IncomingMessage,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision | None:
        if message.visibility != "private" or len(pending_approval_requests) != 1:
            return None
        request = pending_approval_requests[0]
        proposals = {proposal.proposal_id: proposal for proposal in pending_proposals}
        proposal = proposals.get(request.proposal_id)
        if proposal is None or proposal.kind != "question":
            return None
        temporal_update = parse_temporal_update(message.text, reference_date=message.received_at.date())
        if not self.meaningful_feedback_keys.intersection(temporal_update):
            return None
        return OperatingAgentDecision(
            action="apply_feedback",
            confidence=1.0,
            rationale="Single pending clarification request matched private feedback with concrete slots.",
            proposal_patches=(
                ProposalPatch(
                    request_id=request.request_id,
                    proposal_id=proposal.proposal_id,
                    actor_id=message.sender_id,
                    body=message.text,
                    temporal_update=dict(temporal_update),
                    reason="resolve_pending_question",
                    target_confidence=1.0,
                    evidence_text=message.text,
                    missing_slots=tuple(proposal.missing_slots),
                ),
            ),
        )

    def _natural_feedback_decision(
        self,
        message: IncomingMessage,
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision | None:
        """Fallback semantic patcher for fixture/demo mode.

        Live Codex/OpenAI agents should emit the same proposal-patch envelope
        themselves.  This deterministic baseline exists so local tests and
        credential-free demos still exercise the core's validation/commit path
        instead of mutating state before the operating-agent boundary.
        """

        if message.visibility != "private":
            return None
        candidates = _actionable_feedback_candidates(pending_proposals)
        if not candidates:
            return None

        update_type = ""
        temporal_update: dict[str, str] = {}
        if _has_completion_signal(message.text):
            update_type = "completion"
            temporal_update = {"status": "done", "semantic_update_type": update_type}
        elif _has_progress_signal(message.text):
            update_type = "progress"
            temporal_update = {
                PROGRESS_STATUS_KEY: "partial",
                "semantic_update_type": update_type,
            }
            remaining = _remaining_work_from_progress_text(message.text)
            if _looks_half_done(message.text):
                temporal_update[PROGRESS_PERCENT_KEY] = "50"
            if remaining:
                temporal_update[REMAINING_WORK_KEY] = remaining
        elif _has_deferral_signal(message.text) and not _looks_like_missing_info_deferral(message.text):
            parsed = parse_temporal_update(message.text, reference_date=message.received_at.date())
            if not any(parsed.get(key) for key in ("due_date", "scheduled_date", "time_window")):
                return None
            update_type = "deferral"
            temporal_update = dict(parsed)
            temporal_update["semantic_update_type"] = update_type
        else:
            return None

        proposal = _resolve_feedback_target(candidates, message.text)
        if proposal is None:
            return OperatingAgentDecision(
                action="apply_feedback",
                confidence=0.7,
                rationale=f"Natural {update_type} feedback was detected, but target proposal was ambiguous.",
                proposal_patches=(
                    ProposalPatch(
                        request_id="",
                        proposal_id="",
                        actor_id=message.sender_id,
                        body=message.text,
                        temporal_update=temporal_update,
                        reason=f"ambiguous_{update_type}_target",
                        target_confidence=0.0,
                        evidence_text=message.text,
                        missing_slots=("target_proposal",),
                        needs_clarification=True,
                    ),
                ),
            )

        if update_type == "deferral" and temporal_update.get("scheduled_date") and proposal.kind in {"task", "question"}:
            temporal_update["due_date"] = temporal_update.pop("scheduled_date")

        return OperatingAgentDecision(
            action="apply_feedback",
            confidence=1.0,
            rationale=f"Fallback semantic patch recognized a natural {update_type} update for an approved task.",
            proposal_patches=(
                ProposalPatch(
                    request_id="",
                    proposal_id=proposal.proposal_id,
                    actor_id=message.sender_id,
                    body=message.text,
                    temporal_update=temporal_update,
                    reason=f"natural_{update_type}_feedback",
                    target_confidence=0.9,
                    evidence_text=message.text,
                ),
            ),
        )


def _actionable_feedback_candidates(proposals: Sequence[Proposal]) -> tuple[Proposal, ...]:
    return tuple(
        proposal
        for proposal in proposals
        if proposal.status in {"approved", "applied"}
        and not proposal.missing_slots
        and proposal.kind in {"task", "event", "routine", "question"}
    )


def _resolve_feedback_target(candidates: tuple[Proposal, ...], text: str) -> Proposal | None:
    if not candidates:
        return None
    normalized = text.lower()
    explicit = [
        proposal
        for proposal in candidates
        if proposal.proposal_id.lower() in normalized or _short_id(proposal.proposal_id).lower() in normalized
    ]
    if len(explicit) == 1:
        return explicit[0]
    if len(explicit) > 1:
        return None
    if _has_anaphora_target(text):
        return candidates[0] if len(candidates) == 1 else None
    if not _has_target_evidence(candidates, text):
        return None
    scored = [
        (score, proposal)
        for proposal in candidates
        for score in (_feedback_score(proposal, text),)
        if score > 0
    ]
    return pick_best_candidate(scored, min_score=MIN_TARGET_SCORE, min_gap=MIN_RUNNER_UP_GAP)


def _has_target_evidence(candidates: tuple[Proposal, ...], text: str) -> bool:
    clause_tokens = _semantic_tokens(text) - _GENERIC_FEEDBACK_TOKENS
    if not clause_tokens:
        return False
    for proposal in candidates:
        target_tokens = _target_semantic_tokens(proposal) - _GENERIC_FEEDBACK_TOKENS
        shared = target_tokens & clause_tokens
        if len(shared) >= 2 or any(len(token) >= 3 for token in shared):
            return True
    return False


def _feedback_score(proposal: Proposal, text: str) -> int:
    title_tokens = _semantic_tokens(proposal.title)
    raw_tokens = _semantic_tokens(proposal.raw_text)
    text_tokens = _semantic_tokens(text)
    return 10 * len(title_tokens & text_tokens) + 4 * len((raw_tokens - title_tokens) & text_tokens)


def _target_semantic_tokens(proposal: Proposal) -> set[str]:
    text = " ".join(
        item
        for item in (
            proposal.title,
            proposal.raw_text,
            proposal.metadata.get(PARTICIPANTS_KEY, ""),
            proposal.metadata.get(EXTERNAL_PARTICIPANTS_KEY, ""),
            proposal.metadata.get(PARTICIPANT_LABEL_KEY, ""),
            proposal.metadata.get("materials", ""),
        )
        if item
    )
    return _semantic_tokens(text)


def _semantic_tokens(text: str) -> set[str]:
    stopwords = {"오늘", "내일", "이번주", "다음주", "담당자", "일정", "작업", "완료", "했어", "남음"}
    tokens: set[str] = set()
    for token in re.split(r"[^0-9A-Za-z가-힣]+", text.lower()):
        if len(token) < 2 or token in stopwords:
            continue
        tokens.add(token)
        for suffix in ("하기", "하다", "해두기", "정리하기", "제작", "확인"):
            if token.endswith(suffix) and len(token) > len(suffix) + 1:
                tokens.add(token[: -len(suffix)])
    return {token for token in tokens if len(token) >= 2 and token not in stopwords}


_GENERIC_FEEDBACK_TOKENS = {
    "확인",
    "필요",
    "정리",
    "자료",
    "초안",
    "공유",
    "검토",
}


def _has_completion_signal(text: str) -> bool:
    compact = text.replace(" ", "").lower()
    if any(token in compact for token in ("못했", "안했", "미완", "아직", "남음", "남았", "남은", "반쯤", "반만", "일부")):
        return False
    return any(
        token in compact
        for token in ("완료", "끝냈", "끝냄", "끝났", "다했", "다함", "마쳤", "처리했", "해뒀", "했어", "쌌어", "보냈어")
    )


def _has_progress_signal(text: str) -> bool:
    compact = text.replace(" ", "").lower()
    return any(token in compact for token in ("반쯤", "반만", "절반", "일부", "초안", "진행중", "남음", "남았"))


def _looks_half_done(text: str) -> bool:
    compact = text.replace(" ", "").lower()
    return any(token in compact for token in ("반쯤", "반만", "절반", "50%"))


def _remaining_work_from_progress_text(text: str) -> str:
    match = re.search(r"(.+?)(?:만\s*)?남(?:음|았|았어|았습니다)?", text)
    if match:
        fragment = match.group(1).split("고")[-1].strip(" ,.")
        if fragment:
            return fragment
    if "아직" in text:
        return text.split("아직", 1)[1].strip(" ,.。")
    return ""


def _has_deferral_signal(text: str) -> bool:
    compact = text.replace(" ", "").lower()
    return any(token in compact for token in ("미뤄", "연기", "나중에", "다시잡", "리마인드", "다시물어"))


def _looks_like_missing_info_deferral(text: str) -> bool:
    compact = text.replace(" ", "").lower()
    return any(token in compact for token in ("정해지면알려", "나중에알려", "필요하지않", "안정해졌"))


def _has_anaphora_target(text: str) -> bool:
    compact = text.replace(" ", "").lower()
    return any(token in compact for token in ("그거", "그건", "이거", "이건", "방금", "위일정", "해당일정"))


def _short_id(value: str) -> str:
    return value.rsplit("/", 1)[-1][:12]


def decision_to_payload(decision: OperatingAgentDecision) -> dict[str, object]:
    payload = {
        "schema": decision.schema,
        "action": decision.action,
        "source": decision.source,
        "confidence": decision.confidence,
        "rationale": decision.rationale,
        "proposal_drafts": [draft.to_payload() for draft in decision.proposal_drafts],
        "proposal_patches": [patch.to_payload() for patch in decision.proposal_patches],
        "clarification_questions": [question.to_payload() for question in decision.clarification_questions],
    }
    validate_operating_decision_payload(payload)
    return payload


def decision_from_payload(payload: Mapping[str, object]) -> OperatingAgentDecision:
    validate_operating_decision_payload(payload)
    return OperatingAgentDecision(
        action=_operating_action(payload["action"]),
        source=str(payload["source"]),
        confidence=float(payload["confidence"]),
        rationale=str(payload["rationale"]),
        proposal_drafts=tuple(_draft_from_payload(item) for item in _list_of_dicts(payload["proposal_drafts"])),
        proposal_patches=tuple(_patch_from_payload(item) for item in _list_of_dicts(payload["proposal_patches"])),
        clarification_questions=tuple(
            _question_from_payload(item) for item in _list_of_dicts(payload["clarification_questions"])
        ),
        schema=str(payload["schema"]),
    )


def validate_operating_decision_payload(payload: Mapping[str, object]) -> None:
    required = set(OPERATING_DECISION_OUTPUT_SCHEMA["required"])  # type: ignore[index]
    actual = set(payload)
    if actual != required:
        missing = sorted(required - actual)
        extra = sorted(actual - required)
        raise ValueError(f"operating decision keys mismatch; missing={missing}, extra={extra}")
    if payload["schema"] != OPERATING_AGENT_SCHEMA:
        raise ValueError(f"unsupported operating decision schema: {payload['schema']!r}")
    if payload["action"] not in {"create_proposals", "apply_feedback", "no_action"}:
        raise ValueError(f"unsupported operating action: {payload['action']!r}")
    confidence = payload["confidence"]
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError(f"confidence must be between 0 and 1: {confidence!r}")
    for key in ("proposal_drafts", "proposal_patches", "clarification_questions"):
        if not isinstance(payload[key], list):
            raise ValueError(f"{key} must be a list")
    if payload["action"] == "create_proposals" and not payload["proposal_drafts"]:
        raise ValueError("create_proposals requires at least one proposal draft")
    if payload["action"] == "apply_feedback" and not payload["proposal_patches"]:
        raise ValueError("apply_feedback requires at least one proposal patch")
    for item in _list_of_dicts(payload["proposal_drafts"]):
        _validate_keys(item, PROPOSAL_DRAFT_OUTPUT_SCHEMA, "proposal_drafts[]")
        _validate_choice(item["assigned_to"], ASSIGNEE_VALUES, "proposal_drafts[].assigned_to")
        _validate_choice(item["item_type"], PROPOSAL_KIND_VALUES, "proposal_drafts[].item_type")
        _parse_optional_date(str(item["due_date"]), "due_date")
        _parse_optional_date(str(item["scheduled_date"]), "scheduled_date")
    for item in _list_of_dicts(payload["proposal_patches"]):
        _validate_keys(item, PROPOSAL_PATCH_OUTPUT_SCHEMA, "proposal_patches[]")
        target_confidence = item["target_confidence"]
        if not isinstance(target_confidence, (int, float)) or not 0 <= target_confidence <= 1:
            raise ValueError(f"proposal patch target_confidence must be between 0 and 1: {target_confidence!r}")
        if not isinstance(item["needs_clarification"], bool):
            raise ValueError("proposal patch needs_clarification must be a boolean")
        for array_key in ("assumptions", "missing_slots"):
            if not isinstance(item[array_key], list) or any(not isinstance(value, str) for value in item[array_key]):
                raise ValueError(f"proposal patch {array_key} must be a list of strings")
    for item in _list_of_dicts(payload["clarification_questions"]):
        _validate_keys(item, CLARIFICATION_QUESTION_OUTPUT_SCHEMA, "clarification_questions[]")


_LOCATION_OPTIONAL_TOKENS = (
    "배석", "발표", "리뷰위원회", "퇴근", "귀가", "출근", "외출", "외근", "재택",
)


def _normalize_draft_metadata(metadata: dict[str, str], *, raw_text: str, title: str) -> dict[str, str]:
    """Deterministic metadata normalization shared by every operating-agent backend.

    This lives in the core (not in a single CLI adapter) so claude/codex/openai
    all produce identical proposals from the same semantic intent — the project's
    "swap only the brain" contract.
    """
    normalized = dict(metadata)
    attendees = normalized.get(ATTENDEES_KEY, "")
    if attendees and not normalized.get(EXTERNAL_PARTICIPANTS_KEY):
        normalized[EXTERNAL_PARTICIPANTS_KEY] = attendees
    if attendees and not normalized.get(PARTICIPANT_LABEL_KEY):
        normalized[PARTICIPANT_LABEL_KEY] = attendees
    text = f"{raw_text} {title} {attendees}"
    if not normalized.get(PARTICIPANTS_KEY) and ("나" in text or "me" in text):
        normalized[PARTICIPANTS_KEY] = "me"
    if (
        not normalized.get(LOCATION_KEY)
        and normalized.get(LOCATION_OPTIONAL_KEY) != "true"
        and any(token in text for token in _LOCATION_OPTIONAL_TOKENS)
    ):
        normalized[LOCATION_OPTIONAL_KEY] = "true"
    return normalized


def _draft_from_payload(item: Mapping[str, Any]) -> ProposalDraft:
    metadata = item["metadata"]
    if not isinstance(metadata, Mapping):
        raise ValueError("proposal draft metadata must be an object")
    if not isinstance(item["line_number"], int):
        raise ValueError("proposal draft line_number must be an integer")
    if not isinstance(item["needs_review"], bool):
        raise ValueError("proposal draft needs_review must be a boolean")
    _validate_string_values(metadata, "proposal draft metadata")
    return ProposalDraft(
        source_key=str(item["source_key"]),
        raw_text=str(item["raw_text"]),
        title=str(item["title"]),
        discussion_id=str(item["discussion_id"]),
        message_id=str(item["message_id"]),
        line_number=int(item["line_number"]),
        speaker=str(item["speaker"]),
        assigned_to=str(item["assigned_to"]),
        task_management_area=str(item["task_management_area"]),
        due_date=_parse_optional_date(str(item["due_date"]), "due_date"),
        scheduled_date=_parse_optional_date(str(item["scheduled_date"]), "scheduled_date"),
        time_window=str(item["time_window"]),
        task_status=str(item["task_status"]),
        item_type=str(item["item_type"]),
        disposition=str(item["disposition"]),
        needs_review=bool(item["needs_review"]),
        source_url=str(item["source_url"]),
        source_export_path=str(item["source_export_path"]),
        metadata=_normalize_draft_metadata(
            {str(key): str(value) for key, value in metadata.items()},
            raw_text=str(item["raw_text"]),
            title=str(item["title"]),
        ),
    )


def _patch_from_payload(item: Mapping[str, Any]) -> ProposalPatch:
    temporal_update = item["temporal_update"]
    if not isinstance(temporal_update, Mapping):
        raise ValueError("proposal patch temporal_update must be an object")
    _validate_string_values(temporal_update, "proposal patch temporal_update")
    return ProposalPatch(
        request_id=str(item["request_id"]),
        proposal_id=str(item["proposal_id"]),
        actor_id=str(item["actor_id"]),
        body=str(item["body"]),
        temporal_update={str(key): str(value) for key, value in temporal_update.items()},
        reason=str(item["reason"]),
        target_confidence=float(item["target_confidence"]),
        evidence_text=str(item["evidence_text"]),
        assumptions=tuple(str(value) for value in item["assumptions"]),
        missing_slots=tuple(str(value) for value in item["missing_slots"]),
        needs_clarification=bool(item["needs_clarification"]),
    )


def _question_from_payload(item: Mapping[str, Any]) -> ClarificationQuestion:
    missing_slots = item["missing_slots"]
    if not isinstance(missing_slots, list):
        raise ValueError("clarification question missing_slots must be a list")
    if any(not isinstance(value, str) for value in missing_slots):
        raise ValueError("clarification question missing_slots must contain strings")
    return ClarificationQuestion(
        recipient_id=str(item["recipient_id"]),
        prompt=str(item["prompt"]),
        proposal_id=str(item["proposal_id"]),
        missing_slots=tuple(str(value) for value in missing_slots),
    )


def _operating_action(value: object) -> OperatingAction:
    if value not in {"create_proposals", "apply_feedback", "no_action"}:
        raise ValueError(f"unsupported operating action: {value!r}")
    return value  # type: ignore[return-value]


def _list_of_dicts(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"expected list, got {type(value).__name__}")
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"expected object item, got {type(item).__name__}")
    return value


def _validate_keys(item: Mapping[str, object], schema: Mapping[str, object], label: str) -> None:
    required = set(schema["required"])  # type: ignore[index]
    actual = set(item)
    if actual != required:
        missing = sorted(required - actual)
        extra = sorted(actual - required)
        raise ValueError(f"{label} keys mismatch; missing={missing}, extra={extra}")


def _validate_choice(value: object, allowed: tuple[str, ...], label: str) -> None:
    if value not in allowed:
        raise ValueError(f"{label} must be one of {allowed}: {value!r}")


def _validate_string_values(value: Mapping[str, object], label: str) -> None:
    bad_keys = [str(key) for key, item in value.items() if not isinstance(item, str)]
    if bad_keys:
        raise ValueError(f"{label} values must be strings; bad_keys={bad_keys}")


def _parse_optional_date(value: str, field_name: str) -> date | None:
    if value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO date or empty string: {value!r}") from exc
