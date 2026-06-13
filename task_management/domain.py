from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal


ADAPTER_SCHEMA = "task-task_management.discussion-adapter.v1"
TASK_EXPORT_SCHEMA = "task-core.export.v1"
ASSIGNEE_VALUES = ("me", "teammate", "shared", "unassigned")


@dataclass(frozen=True)
class KindSpec:
    """Per-proposal-kind behavior, centralized so a new kind is a one-row change.

    Every field reproduces today's scattered set/branch literals exactly; see the
    migrated call sites for the source of each value. Behavior-preserving only.
    """

    name: str
    schedulable: bool
    requires_date: bool
    required_event_slots: tuple[str, ...]
    auto_approve_on_intake: bool
    conflict_participant: bool
    duplicate_merge_participant: bool
    export_item_type: str
    export_disposition: str
    dashboard_section: str
    reminder_message_type: str = ""


# One row per existing kind. Field values reproduce current behavior exactly:
#   - schedulable / dashboard_section: scheduled-commitment + dashboard kind
#     filters (work_item_state.is_scheduled_commitment requires kind == "event";
#     frontend/slack_page sections key off kind == question/routine/reference).
#   - requires_date: slot_validator date-requirement exclusion set; tasks/events
#     require a date, the rest do not (reference/routine/decision/question).
#   - required_event_slots: slot_validator event/routine slot sets.
#   - auto_approve_on_intake: approval_policy auto-approves only reference.
#   - conflict_participant: conflict_policy participates only event/routine.
#   - duplicate_merge_participant: workflow_normalizer merges only task/event.
#   - export_item_type/export_disposition: task_core_bridge whitelist mapping.
#   - reminder_message_type: reminders message_type per approved routine/event.
KIND_SPECS: dict[str, "KindSpec"] = {
    "task": KindSpec(
        name="task",
        schedulable=False,
        requires_date=True,
        required_event_slots=(),
        auto_approve_on_intake=False,
        conflict_participant=False,
        duplicate_merge_participant=True,
        export_item_type="task",
        export_disposition="execution",
        dashboard_section="",
        reminder_message_type="",
    ),
    "event": KindSpec(
        name="event",
        schedulable=True,
        requires_date=True,
        required_event_slots=("participants", "time", "location"),
        auto_approve_on_intake=False,
        conflict_participant=True,
        duplicate_merge_participant=True,
        export_item_type="event",
        export_disposition="execution",
        dashboard_section="",
        reminder_message_type="event_reminder",
    ),
    "routine": KindSpec(
        name="routine",
        schedulable=False,
        requires_date=False,
        required_event_slots=("recurrence", "time", "location", "participants"),
        auto_approve_on_intake=False,
        conflict_participant=True,
        duplicate_merge_participant=False,
        export_item_type="routine",
        export_disposition="execution",
        dashboard_section="routines",
        reminder_message_type="routine_reminder",
    ),
    "reference": KindSpec(
        name="reference",
        schedulable=False,
        requires_date=False,
        required_event_slots=(),
        auto_approve_on_intake=True,
        conflict_participant=False,
        duplicate_merge_participant=False,
        export_item_type="reference",
        export_disposition="reference",
        dashboard_section="references",
        reminder_message_type="",
    ),
    "question": KindSpec(
        name="question",
        schedulable=False,
        requires_date=False,
        required_event_slots=(),
        auto_approve_on_intake=False,
        conflict_participant=False,
        duplicate_merge_participant=False,
        export_item_type="task",
        export_disposition="execution",
        dashboard_section="questions",
        reminder_message_type="",
    ),
    "decision": KindSpec(
        name="decision",
        schedulable=False,
        requires_date=False,
        required_event_slots=(),
        auto_approve_on_intake=False,
        conflict_participant=False,
        duplicate_merge_participant=False,
        export_item_type="task",
        export_disposition="execution",
        dashboard_section="",
        reminder_message_type="",
    ),
}

PROPOSAL_KIND_VALUES = tuple(KIND_SPECS)


@dataclass(frozen=True)
class TeamTaskMessage:
    discussion_id: str
    message_id: str
    line_number: int
    speaker: str
    text: str


@dataclass(frozen=True)
class TeamTaskTaskCandidate:
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


ProposalStatus = Literal[
    "draft",
    "posted",
    "awaiting_approval",
    "approved",
    "rejected",
    "applied",
    "done",
]
# Keep this Literal in sync with KIND_SPECS / PROPOSAL_KIND_VALUES (a Literal
# cannot be built from a runtime tuple). The registry-consistency test locks the
# two together so this annotation cannot silently drift.
ProposalKind = Literal["task", "event", "routine", "reference", "question", "decision"]
ApprovalDecisionValue = Literal["accepted", "rejected"]
MessageVisibility = Literal["private", "team"]
FrontendSurface = Literal["personal_chat", "team_room", "web_task_page"]
ChatCommandAction = Literal["accept", "reject", "change", "complete", "assign"]


@dataclass(frozen=True)
class Actor:
    actor_id: str
    display_name: str
    aliases: tuple[str, ...] = ()
    is_default_approver: bool = True


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    sender_id: str
    chat_id: str
    visibility: MessageVisibility
    text: str
    received_at: datetime
    # Recent DM turns (user + bot), most recent last, so the semantic agent can
    # resolve a reply against the prior conversation (e.g. an affirmative answer to
    # the bot's own pending question). Optional; defaults to empty for all callers.
    recent_conversation: tuple = ()


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    source_message_id: str
    proposer_id: str
    title: str
    raw_text: str
    kind: ProposalKind
    status: ProposalStatus
    assigned_to: str
    task_management_area: str
    discussion_id: str
    message_id: str
    required_approvers: tuple[str, ...] = ()
    approvals: tuple[str, ...] = ()
    missing_slots: tuple[str, ...] = ()
    due_date: date | None = None
    scheduled_date: date | None = None
    time_window: str = ""
    source_url: str = ""
    source_export_path: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    proposal_id: str
    approver_id: str
    status: Literal["pending", "accepted", "rejected"] = "pending"
    requested_at: datetime | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True)
class ApprovalDecision:
    request_id: str
    proposal_id: str
    approver_id: str
    decision: ApprovalDecisionValue
    decided_at: datetime


@dataclass(frozen=True)
class OutboundMessage:
    surface: FrontendSurface
    recipient_id: str
    message_type: str
    text: str
    proposal_id: str = ""
    approval_request_id: str = ""
    card: dict[str, str] = field(default_factory=dict)

    @property
    def channel(self) -> str:
        """Backward-compatible surface category for older simulator tests."""

        if self.surface == "personal_chat":
            return "private"
        if self.surface == "team_room":
            return "team"
        return "web"


@dataclass(frozen=True)
class OrchestrationResult:
    proposals: tuple[Proposal, ...] = ()
    approval_requests: tuple[ApprovalRequest, ...] = ()
    outbound_messages: tuple[OutboundMessage, ...] = ()
    ignored_duplicate: bool = False


@dataclass(frozen=True)
class ChatCommand:
    action: ChatCommandAction
    target_id: str
    body: str = ""
