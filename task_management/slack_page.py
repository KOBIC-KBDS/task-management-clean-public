from __future__ import annotations

from .relations import (
    DATE_WINDOW_END_KEY,
    DATE_WINDOW_LABEL_KEY,
    DATE_WINDOW_START_KEY,
    LINK_PREP_SUBTASK,
    LINK_TYPE_KEY,
    LOCATION_KEY,
)

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .domain import KIND_SPECS, ApprovalRequest, Proposal
from .human_view import (
    actor_label,
    date_label,
    date_window_display_label,
    missing_slot_label,
    proposal_participants_label,
    short_id,
)
from .sort_keys import schedule_first_sort_key
from .store import TeamTaskStore
from .work_item_state import is_personal_scope, schedule_first_date


@dataclass(frozen=True)
class SlackMonthlyTaskPageModel:
    actor_id: str
    month: date
    approved: tuple[Proposal, ...]
    pending_approvals: tuple[tuple[ApprovalRequest, Proposal | None], ...]
    floating: tuple[Proposal, ...]
    routines: tuple[Proposal, ...]
    prep_subtasks: tuple[Proposal, ...]
    references: tuple[Proposal, ...]


def build_slack_monthly_task_page_model(
    store: TeamTaskStore,
    *,
    actor_id: str,
    month: date,
) -> SlackMonthlyTaskPageModel:
    """Build a Slack personal monthly page model from local state only."""

    month_start, month_end = _month_bounds(month)
    proposals = store.list_proposals()
    proposal_by_id = {item.proposal_id: item for item in proposals}
    scoped = tuple(item for item in proposals if _is_personal_scope(item, actor_id))

    approved = tuple(
        sorted(
            (
                item
                for item in scoped
                if item.status in {"approved", "applied", "done"}
                and _proposal_date(item) is not None
                and month_start <= _proposal_date(item) <= month_end  # type: ignore[operator]
                and item.kind not in {"reference", "routine"}
                and item.metadata.get(LINK_TYPE_KEY) != LINK_PREP_SUBTASK
            ),
            key=_sort_key,
        )
    )
    pending_approvals = tuple(
        (request, proposal_by_id.get(request.proposal_id))
        for request in store.list_approval_requests(approver_id=actor_id, status="pending")
    )
    floating = tuple(
        sorted(
            (
                item
                for item in scoped
                if item.status in {"draft", "posted", "awaiting_approval"}
                or item.missing_slots
            ),
            key=_sort_key,
        )
    )
    routines = tuple(sorted((item for item in scoped if KIND_SPECS[item.kind].dashboard_section == "routines"), key=_sort_key))
    prep_subtasks = tuple(
        sorted((item for item in scoped if item.metadata.get(LINK_TYPE_KEY) == LINK_PREP_SUBTASK), key=_sort_key)
    )
    references = tuple(sorted((item for item in scoped if KIND_SPECS[item.kind].dashboard_section == "references"), key=_sort_key))

    return SlackMonthlyTaskPageModel(
        actor_id=actor_id,
        month=month_start,
        approved=approved,
        pending_approvals=pending_approvals,
        floating=floating,
        routines=routines,
        prep_subtasks=prep_subtasks,
        references=references,
    )


def render_slack_monthly_task_page_markdown(model: SlackMonthlyTaskPageModel) -> str:
    """Render a Markdown body suitable for a Slack personal Canvas/monthly page."""

    title = f"{model.month.year}년 {model.month.month}월 개인 Task Management"
    lines = [
        f"# {title}",
        "",
        "이 페이지는 Slack 개인 DM으로 들어온 일을 월별로 정리하는 작업대입니다. DM은 입력과 피드백, 이 페이지는 체크와 정리, 웹 task page는 전체 현황과 감사 로그 확인에 둡니다.",
        "",
        "## 이번 달 확정 일정",
        *_proposal_checklist(model.approved, empty="이번 달 확정 일정이 없습니다."),
        "",
        "## 승인/확인 필요",
        *_pending_approval_lines(model.pending_approvals),
        "",
        "## 떠 있는 항목",
        *_proposal_checklist(model.floating, empty="떠 있는 항목이 없습니다."),
        "",
        "## 반복 루틴",
        *_proposal_checklist(model.routines, empty="반복 루틴이 없습니다."),
        "",
        "## 준비 작업",
        *_proposal_checklist(model.prep_subtasks, empty="준비 작업이 없습니다."),
        "",
        "## 참고 링크",
        *_reference_lines(model.references),
        "",
        "## DM 피드백 규칙",
        "수락, 거절, 변경, 완료 같은 짧은 답장을 Slack 개인 DM에 남기면 운영 agent가 proposal 상태를 갱신합니다. 예를 들어 `변경 <요청ID> 토요일 오전 나랑 팀원 같이` 또는 `완료 <proposalID>`처럼 씁니다.",
    ]
    return "\n".join(lines).strip() + "\n"


def _proposal_checklist(proposals: Iterable[Proposal], *, empty: str) -> list[str]:
    items = list(proposals)
    if not items:
        return [empty]
    return [_proposal_line(item) for item in items]


def _proposal_line(proposal: Proposal) -> str:
    marker = "x" if proposal.status in {"done", "applied"} else " "
    chunks = [_md_escape(proposal.title)]
    when = _when_label(proposal)
    if when:
        chunks.append(when)
    if proposal.assigned_to:
        chunks.append(f"담당 {_md_escape(actor_label(proposal.assigned_to))}")
    participants = _participants_label(proposal)
    if participants:
        chunks.append(f"참석 {_md_escape(participants)}")
    location = proposal.metadata.get(LOCATION_KEY, "")
    if location:
        chunks.append(f"장소 {_md_escape(location)}")
    if proposal.missing_slots:
        chunks.append("확인 필요 " + _md_escape(", ".join(missing_slot_label(slot) for slot in proposal.missing_slots)))
    chunks.append(f"`{short_id(proposal.proposal_id)}`")
    return f"- [{marker}] " + " · ".join(chunks)


def _pending_approval_lines(items: Iterable[tuple[ApprovalRequest, Proposal | None]]) -> list[str]:
    rows = list(items)
    if not rows:
        return ["승인/확인 대기 항목이 없습니다."]
    lines = []
    for request, proposal in rows:
        title = proposal.title if proposal is not None else request.proposal_id
        when = _when_label(proposal) if proposal is not None else ""
        suffix = f" · {when}" if when else ""
        lines.append(
            f"- [ ] {_md_escape(title)}{suffix} · 요청 `{short_id(request.request_id)}` · 답장 `수락 {request.request_id}` / `거절 {request.request_id}`"
        )
    return lines


def _reference_lines(proposals: Iterable[Proposal]) -> list[str]:
    items = list(proposals)
    if not items:
        return ["참고 링크가 없습니다."]
    lines = []
    for proposal in items:
        url = proposal.source_url or proposal.metadata.get("url", "")
        url_label = f" · {url}" if url.startswith("http://") or url.startswith("https://") else ""
        lines.append(f"- {_md_escape(proposal.title)}{url_label} · `{short_id(proposal.proposal_id)}`")
    return lines


def _month_bounds(month: date) -> tuple[date, date]:
    start = month.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    return start, date.fromordinal(next_month.toordinal() - 1)


_proposal_date = schedule_first_date
_sort_key = schedule_first_sort_key
_is_personal_scope = is_personal_scope


def _when_label(proposal: Proposal | None) -> str:
    if proposal is None:
        return ""
    proposal_date = _proposal_date(proposal)
    parts = []
    if proposal_date is not None:
        parts.append(date_label(proposal_date))
    if proposal.time_window:
        parts.append(proposal.time_window)
    if proposal.metadata.get(DATE_WINDOW_START_KEY) and proposal.metadata.get(DATE_WINDOW_END_KEY):
        parts.append(
            date_window_display_label(
                proposal.metadata[DATE_WINDOW_START_KEY],
                proposal.metadata[DATE_WINDOW_END_KEY],
                proposal.metadata.get(DATE_WINDOW_LABEL_KEY, ""),
            )
        )
    return " ".join(parts)


def _participants_label(proposal: Proposal) -> str:
    return proposal_participants_label(proposal)


def _md_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()
