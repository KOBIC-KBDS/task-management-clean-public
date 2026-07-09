from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from .domain import Proposal
from .human_view import (
    build_missing_slot_question,
    date_label,
    render_confirmed_sentence,
    render_missing_slot_sentence,
)
from .sort_keys import schedule_first_sort_key
from .store import TeamTaskStore
from .work_item_state import is_personal_scope, is_surface_visible_item, schedule_first_date


def build_today_update_digest(
    store: TeamTaskStore,
    *,
    now: datetime,
    actor_id: str = "me",
    dashboard_url: str = "",
    canvas_url: str = "",
    max_chars: int = 3900,
) -> str:
    """Render a concise Slack DM digest for the current personal task cycle."""

    today = now.date()
    proposals = [
        proposal
        for proposal in store.list_proposals()
        if _is_personal_scope(proposal, actor_id) and is_surface_visible_item(proposal)
    ]
    touched = [proposal for proposal in proposals if _is_touched_today(proposal, today)]
    confirmed = [
        proposal
        for proposal in touched
        if proposal.status in {"approved", "applied", "done"} and not proposal.missing_slots
    ]
    floating = [
        proposal
        for proposal in proposals
        if proposal.status in {"draft", "posted", "awaiting_approval"} or proposal.missing_slots
    ]
    today_pending = [
        proposal
        for proposal in floating
        if _proposal_date(proposal) == today or _is_touched_today(proposal, today)
    ]
    pending_requests = [
        (request, next((proposal for proposal in proposals if proposal.proposal_id == request.proposal_id), None))
        for request in store.list_approval_requests(approver_id=actor_id, status="pending")
    ]

    clarification_lines = _clarification_lines(pending_requests, today_pending)

    lines: list[str] = [
        f"*오늘 Task 업데이트* · {date_label(today)}",
        "",
        "*확정된 일정과 준비 작업*",
        *_confirmed_lines(confirmed, empty="- 오늘 새로 확정되거나 준비할 항목은 없습니다."),
        "",
        "*확인이 필요한 항목*",
        *clarification_lines,
    ]
    link_lines = []
    if dashboard_url:
        link_lines.append(f"- 웹 task page: {dashboard_url}")
    if canvas_url:
        link_lines.append(f"- Slack Canvas snapshot: {canvas_url}")
    if link_lines:
        lines.extend(["", "*바로가기*", *link_lines])
    lines.extend(
        [
            "",
            "답장은 자연어로 남겨도 됩니다. 예: `변경 approval/xxxx 다음 주 화요일 오전`, `완료 proposalID`",
            "아직 정해지지 않은 항목은 다음 업데이트 때 다시 물어볼게요.",
        ]
    )
    digest = "\n".join(lines).strip()
    if len(digest) <= max_chars:
        return digest
    return digest[: max_chars - 80].rstrip() + "\n…\n(길어서 일부만 표시했습니다. 전체 목록은 Web/Canvas에서 확인하세요.)"


def _confirmed_lines(proposals: Iterable[Proposal], *, empty: str) -> list[str]:
    items = sorted(proposals, key=_sort_key)
    if not items:
        return [empty]
    return [f"- {_confirmed_sentence(proposal)}" for proposal in items]


def _clarification_lines(items, floating: Iterable[Proposal]) -> list[str]:
    rows = [(request, proposal) for request, proposal in items if proposal is not None]
    by_proposal = {proposal.proposal_id: request for request, proposal in rows}
    floating_items = [proposal for proposal in sorted(floating, key=_sort_key) if proposal.missing_slots]
    seen: set[str] = set()
    lines: list[str] = []
    for request, proposal in rows:
        if proposal.proposal_id in seen:
            continue
        seen.add(proposal.proposal_id)
        lines.append(f"- {_clarification_sentence(proposal, request.request_id)}")
    for proposal in floating_items:
        if proposal.proposal_id in seen:
            continue
        seen.add(proposal.proposal_id)
        request = by_proposal.get(proposal.proposal_id)
        lines.append(f"- {_clarification_sentence(proposal, request.request_id if request else '')}")
    if not lines:
        return ["- 지금 바로 확인받을 항목은 없습니다."]
    return lines


def _confirmed_sentence(proposal: Proposal) -> str:
    return render_confirmed_sentence(proposal)


def _clarification_sentence(proposal: Proposal, request_id: str) -> str:
    return render_missing_slot_sentence(build_missing_slot_question(proposal, request_id=request_id))


def _is_touched_today(proposal: Proposal, today: date) -> bool:
    return any(
        value is not None and value.date() == today
        for value in (proposal.created_at, proposal.updated_at)
    ) or _proposal_date(proposal) == today


_proposal_date = schedule_first_date
_sort_key = schedule_first_sort_key
_is_personal_scope = is_personal_scope
