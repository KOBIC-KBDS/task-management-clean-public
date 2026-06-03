from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

from .attention import (
    HumanAttentionItem,
    collect_human_attention_items,
    deferred_missing_slots,
    has_deferred_missing_info,
)
from .domain import OutboundMessage, Proposal
from .human_view import (
    actor_label,
    build_missing_slot_question,
    date_label,
    human_when_label,
    proposal_status_icon,
    proposal_status_label,
    render_confirmed_sentence,
    render_missing_slot_sentence,
    short_id,
)
from .relations import (
    WORKFLOW_PARENT_ROLE,
    WORKFLOW_ROLE_KEY,
    blocking_dependencies,
    child_proposals,
    is_relation_complete,
    parent_proposal_id,
    step_label,
)
from .sort_keys import proposal_deadline_sort_key
from .store import TeamTaskStore
from .work_item_state import (
    is_past_scheduled_commitment,
    needs_time_resolution,
    requires_progress_confirmation,
    work_item_due_detail_label,
    work_item_urgency_label,
)


def build_morning_briefing(
    store: TeamTaskStore,
    *,
    now: datetime,
    actor_id: str = "me",
    dashboard_url: str = "",
    reserve: bool = True,
    max_chars: int = 3900,
) -> tuple[OutboundMessage, ...]:
    """Build one deduped personal morning briefing for the actor.

    This is the proactive-secretary view, not a task mutation path.  It reads the
    current state, renders Korean guidance, and optionally reserves the outbound
    dedupe key so repeated scheduler runs do not nag.
    """

    today = now.date()
    dedupe_key = morning_briefing_dedupe_key(actor_id, today)
    if reserve and store.has_outbound_delivery(dedupe_key):
        return ()

    all_proposals = list(store.list_proposals())
    proposals = [proposal for proposal in all_proposals if _is_personal_scope(proposal, actor_id)]
    due_tasks = [
        proposal
        for proposal in proposals
        if proposal.status in {"approved", "applied", "awaiting_approval"}
        and proposal.status != "done"
        and (
            (proposal.kind in {"task", "question", "routine"} and proposal.due_date is not None and proposal.due_date <= today)
            or is_past_scheduled_commitment(proposal, today=today)
        )
        and not has_deferred_missing_info(proposal)
    ]
    due_task_ids = {proposal.proposal_id for proposal in due_tasks}
    today_confirmed = [
        proposal
        for proposal in proposals
        if proposal.status in {"approved", "applied"} and _proposal_date(proposal) == today
        and not has_deferred_missing_info(proposal)
        and proposal.proposal_id not in due_task_ids
    ]
    overdue = [proposal for proposal in due_tasks if work_item_urgency_label(proposal, today=today)]
    pending_requests = [
        (request, store.get_proposal(request.proposal_id))
        for request in store.list_approval_requests(approver_id=actor_id, status="pending")
    ]
    pending = [proposal for _, proposal in pending_requests if proposal is not None and proposal.missing_slots]
    attention_items = collect_human_attention_items(
        proposals,
        pending_requests,
        today=today,
        now=now,
        dependency_proposals=all_proposals,
    )
    deferred_due = [
        item.proposal
        for item in attention_items
        if item.kind == "deferred_reminder"
    ]

    lines = [
        f"*오늘 아침 브리핑* · {date_label(today)}",
        "",
        "*오늘 일정/할 일*",
        *_proposal_lines(
            today_confirmed,
            empty="- 오늘 날짜로 확정된 일정/작업은 없습니다.",
            all_proposals=proposals,
            today=today,
        ),
        "",
        "*오늘 챙길 작업*",
        *_proposal_lines(
            due_tasks,
            empty="- 오늘 마감으로 잡힌 미완료 작업은 없습니다.",
            all_proposals=proposals,
            today=today,
        ),
    ]
    if overdue:
        lines.extend(
            [
                "",
                "*밀린 작업*",
                *_proposal_lines(overdue, empty="- 밀린 작업은 없습니다.", all_proposals=proposals, today=today),
            ]
        )
    lines.extend(["", "*오늘 답장/확인 필요한 항목*", *_attention_lines(attention_items, today=today)])
    if dashboard_url:
        lines.extend(["", f"웹 task page: {dashboard_url}"])
    lines.extend(
        [
            "",
            "답장은 자연어로 해도 됩니다.",
            "예: `출장 준비물 다 쌌어`, `ProjectA 정리는 금요일 오후로 미뤄줘`, `장소는 3층 회의실`",
        ]
    )
    text = _truncate("\n".join(lines).strip(), max_chars=max_chars)
    message = OutboundMessage(
        surface="personal_chat",
        recipient_id=actor_id,
        message_type="morning_briefing",
        text=text,
        card={
            "dedupe_key": dedupe_key,
            "date": today.isoformat(),
            "confirmed_count": str(len(today_confirmed)),
            "due_task_count": str(len(due_tasks)),
            "pending_count": str(len(pending)),
            "attention_count": str(len(attention_items)),
            "deferred_due_count": str(len(deferred_due)),
        },
    )
    if reserve:
        recorded = store.record_outbound_delivery(
            dedupe_key=dedupe_key,
            surface=message.surface,
            recipient_id=message.recipient_id,
            provider="slack",
            provider_message_id="morning-briefing-preview",
            sent_at=now,
            payload={"text": message.text, "card": message.card},
        )
        if not recorded:
            return ()
        store.append_event(
            "briefing.morning.created",
            {"dedupe_key": dedupe_key, "actor_id": actor_id, "date": today.isoformat()},
            occurred_at=now,
        )
    return (message,)


def build_afternoon_briefing(
    store: TeamTaskStore,
    *,
    now: datetime,
    actor_id: str = "me",
    dashboard_url: str = "",
    reserve: bool = True,
    max_chars: int = 3900,
) -> tuple[OutboundMessage, ...]:
    """Build one deduped 13:00 personal briefing focused on the rest of today and this week."""

    today = now.date()
    dedupe_key = afternoon_briefing_dedupe_key(actor_id, today)
    if reserve and store.has_outbound_delivery(dedupe_key):
        return ()

    all_proposals = list(store.list_proposals())
    proposals = [proposal for proposal in all_proposals if _is_personal_scope(proposal, actor_id)]
    week_end = today + timedelta(days=6)
    today_open = [
        proposal
        for proposal in proposals
        if proposal.status in {"approved", "applied", "awaiting_approval"}
        and proposal.status != "done"
        and (_proposal_date(proposal) == today or is_past_scheduled_commitment(proposal, today=today))
        and not has_deferred_missing_info(proposal)
    ]
    week_open = [
        proposal
        for proposal in proposals
        if proposal.status in {"approved", "applied", "awaiting_approval"}
        and proposal.status != "done"
        and (proposal_date := _proposal_date(proposal)) is not None
        and today < proposal_date <= week_end
        and not has_deferred_missing_info(proposal)
    ]
    pending_requests = [
        (request, store.get_proposal(request.proposal_id))
        for request in store.list_approval_requests(approver_id=actor_id, status="pending")
    ]
    attention_items = collect_human_attention_items(
        proposals,
        pending_requests,
        today=today,
        now=now,
        dependency_proposals=all_proposals,
    )

    lines = [
        f"*오후 1시 브리핑* · {date_label(today)}",
        "",
        "*오늘 남은 일정/할 일*",
        *_proposal_lines(today_open, empty="- 오늘 남은 날짜 항목은 없습니다.", all_proposals=proposals, today=today),
        "",
        "*이번 주 남은 항목*",
        *_proposal_lines(week_open, empty="- 오늘 이후 7일 안에 잡힌 항목은 없습니다.", all_proposals=proposals, today=today),
        "",
        "*답장/확인 필요한 항목*",
        *_attention_lines(attention_items, today=today),
    ]
    if dashboard_url:
        lines.extend(["", f"웹 task page: {dashboard_url}"])
    lines.extend(
        [
            "",
            "답장은 자연어로 주면 됩니다.",
            "예: `오전 항목 완료`, `오후 미팅 준비는 반쯤 했어`, `내일 오전으로 미뤄줘`",
        ]
    )
    text = _truncate("\n".join(lines).strip(), max_chars=max_chars)
    message = OutboundMessage(
        surface="personal_chat",
        recipient_id=actor_id,
        message_type="afternoon_briefing",
        text=text,
        card={
            "dedupe_key": dedupe_key,
            "date": today.isoformat(),
            "today_open_count": str(len(today_open)),
            "week_open_count": str(len(week_open)),
            "attention_count": str(len(attention_items)),
        },
    )
    if reserve:
        recorded = store.record_outbound_delivery(
            dedupe_key=dedupe_key,
            surface=message.surface,
            recipient_id=message.recipient_id,
            provider="slack",
            provider_message_id="afternoon-briefing-preview",
            sent_at=now,
            payload={"text": message.text, "card": message.card},
        )
        if not recorded:
            return ()
        store.append_event(
            "briefing.afternoon.created",
            {"dedupe_key": dedupe_key, "actor_id": actor_id, "date": today.isoformat()},
            occurred_at=now,
        )
    return (message,)


def build_proactive_checks(
    store: TeamTaskStore,
    *,
    now: datetime,
    actor_id: str = "me",
    reserve: bool = True,
) -> tuple[OutboundMessage, ...]:
    """Ask whether due unfinished work has been done, with per-day dedupe."""

    today = now.date()
    checks: list[OutboundMessage] = []
    all_proposals = list(store.list_proposals())
    proposals_by_id = {proposal.proposal_id: proposal for proposal in all_proposals}
    for proposal in all_proposals:
        if not _is_personal_scope(proposal, actor_id):
            continue
        if proposal.status not in {"approved", "applied"}:
            continue
        if not requires_progress_confirmation(proposal, today=today):
            continue
        if has_deferred_missing_info(proposal):
            continue
        dedupe_key = proactive_check_dedupe_key(proposal, actor_id=actor_id, today=today)
        if reserve and store.has_outbound_delivery(dedupe_key):
            continue
        overdue = bool(work_item_urgency_label(proposal, today=today))
        is_past_event = is_past_scheduled_commitment(proposal, today=today)
        prefix = "지난 일정 확인" if is_past_event else "밀린 작업 확인" if overdue else "오늘 할 일 확인"
        blockers = blocking_dependencies(proposal, proposals_by_id)
        if proposal.due_date is not None:
            date_sentence = f"{proposal.title}은(는) {date_label(proposal.due_date)}까지로 잡혀 있습니다."
        elif proposal.scheduled_date is not None:
            date_sentence = f"{proposal.title}은(는) {date_label(proposal.scheduled_date)} 일정으로 잡혀 있습니다."
        else:
            date_sentence = f"{proposal.title}은(는) 날짜 미정입니다."
        if blockers:
            blocker_titles = ", ".join(f"*{dependency.title}*" for dependency in blockers)
            text = (
                f"*{prefix}*\n"
                f"{date_sentence}\n"
                f"먼저 {blocker_titles} 완료 여부가 확인되어야 합니다. "
                "선행 작업의 완료/진행/연기 상태를 알려주세요."
            )
        else:
            request_sentence = "결과/참석 여부를 알려주세요." if is_past_event else "진행 상황을 알려주세요."
            text = (
                f"*{prefix}*\n"
                f"{date_sentence}\n"
                f"{request_sentence} 완료했다면 예: "
                f"`{proposal.title} 완료`"
            )
        message = OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type="due_work_check",
            text=text,
            proposal_id=proposal.proposal_id,
            card={
                "dedupe_key": dedupe_key,
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "due_date": proposal.due_date.isoformat() if proposal.due_date else "",
                "scheduled_date": proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
                "overdue": str(overdue).lower(),
            },
        )
        if reserve:
            recorded = store.record_outbound_delivery(
                dedupe_key=dedupe_key,
                surface=message.surface,
                recipient_id=message.recipient_id,
                provider="slack",
                provider_message_id="proactive-check-preview",
                sent_at=now,
                payload={"text": message.text, "card": message.card},
            )
            if not recorded:
                continue
            store.append_event(
                "proactive_check.created",
                {
                    "dedupe_key": dedupe_key,
                    "proposal_id": proposal.proposal_id,
                    "actor_id": actor_id,
                    "date": today.isoformat(),
                },
                occurred_at=now,
            )
        checks.append(message)
    return tuple(checks)


def build_end_of_day_review(
    store: TeamTaskStore,
    *,
    now: datetime,
    actor_id: str = "me",
    reserve: bool = True,
    max_chars: int = 3900,
) -> tuple[OutboundMessage, ...]:
    """Build one end-of-day progress review for today's unfinished work.

    The EOD review is the secretary's "do not let today's tasks silently roll
    over" pass.  It excludes proposals already marked done, asks the human to
    either complete/progress/defer each remaining item, and includes pending
    missing-slot proposals whose scheduled/due date has arrived.
    """

    today = now.date()
    dedupe_key = end_of_day_review_dedupe_key(actor_id, today)
    if reserve and store.has_outbound_delivery(dedupe_key):
        return ()

    proposals = [proposal for proposal in store.list_proposals() if _is_personal_scope(proposal, actor_id)]
    due_open = [
        proposal
        for proposal in proposals
        if proposal.status in {"approved", "applied"}
        and proposal.status != "done"
        and proposal.kind in {"task", "question", "routine"}
        and proposal.due_date is not None
        and proposal.due_date <= today
    ]
    today_events = [
        proposal
        for proposal in proposals
        if proposal.status in {"approved", "applied"}
        and proposal.status != "done"
        and proposal.kind == "event"
        and proposal.scheduled_date == today
    ]
    pending_due = [
        proposal
        for proposal in proposals
        if proposal.status == "awaiting_approval"
        and proposal.missing_slots
        and (_proposal_date(proposal) is not None and _proposal_date(proposal) <= today)
    ]
    completed_today = [
        proposal
        for proposal in proposals
        if proposal.status == "done" and _completed_on(proposal, today=today)
    ]

    review_items = sorted(
        [*due_open, *today_events],
        key=lambda proposal: proposal_deadline_sort_key(proposal, today=today, overdue_last=True),
    )
    pending_items = sorted(
        pending_due,
        key=lambda proposal: proposal_deadline_sort_key(proposal, today=today, overdue_last=True),
    )
    if not review_items and not pending_items:
        return ()

    lines = [
        f"*오늘 남은 항목 확인 · {date_label(today)}*",
        "",
        "오늘 남은 개인 항목만 정리합니다.",
        "이미 완료 처리된 항목은 다시 묻지 않습니다.",
        "",
    ]
    if review_items:
        lines.extend(["*남은 항목*"])
        for proposal in review_items:
            due_label = date_label(_proposal_date(proposal)) if _proposal_date(proposal) else "날짜 미정"
            detail = " · ".join(item for item in (due_label, proposal.time_window, proposal.metadata.get("location", "")) if item)
            overdue_detail = " · 🔴 마감 지남" if _is_overdue(proposal, today=today) else ""
            lines.append(f"- *{proposal.title}*" + (f" ({detail}{overdue_detail})" if detail else ""))
    if pending_items:
        lines.extend(["", "*확정에 필요한 정보*"])
        for proposal in pending_items:
            missing = ", ".join(proposal.missing_slots)
            lines.append(f"- *{proposal.title}*: {missing} 필요")
    if completed_today:
        lines.extend(["", "*오늘 완료 처리됨*"])
        lines.extend(f"- {proposal.title}" for proposal in sorted(completed_today, key=_sort_key))
    lines.extend(
        [
            "",
            "답장은 자연어로 주면 됩니다. 예:",
            "- `발표자료 최종본 보냈어`",
            "- `ProjectA 후속 정리는 반쯤 했고 공유만 남음`",
            "- `개인 일정 확인은 내일 오전 10시로 미뤄줘`",
            "",
            "미완료라면 새 마감/다음 확인 시점을 알려주세요. 그러면 기존 task를 닫지 않고 마감 또는 진행상황을 갱신합니다.",
        ]
    )
    text = _truncate("\n".join(lines).strip(), max_chars=max_chars)
    message = OutboundMessage(
        surface="personal_chat",
        recipient_id=actor_id,
        message_type="end_of_day_review",
        text=text,
        card={
            "dedupe_key": dedupe_key,
            "date": today.isoformat(),
            "review_count": str(len(review_items)),
            "pending_count": str(len(pending_items)),
            "completed_today_count": str(len(completed_today)),
        },
    )
    if reserve:
        recorded = store.record_outbound_delivery(
            dedupe_key=dedupe_key,
            surface=message.surface,
            recipient_id=message.recipient_id,
            provider="slack",
            provider_message_id="end-of-day-review-preview",
            sent_at=now,
            payload={"text": message.text, "card": message.card},
        )
        if not recorded:
            return ()
        store.append_event(
            "briefing.end_of_day.created",
            {"dedupe_key": dedupe_key, "actor_id": actor_id, "date": today.isoformat()},
            occurred_at=now,
        )
    return (message,)


def morning_briefing_dedupe_key(actor_id: str, today: date) -> str:
    return f"slack-morning-briefing/{actor_id}/{today.isoformat()}"


def afternoon_briefing_dedupe_key(actor_id: str, today: date) -> str:
    return f"slack-afternoon-briefing/{actor_id}/{today.isoformat()}"


def proactive_check_dedupe_key(proposal: Proposal, *, actor_id: str, today: date) -> str:
    return f"slack-proactive-check/{actor_id}/{proposal.proposal_id}/{today.isoformat()}"


def end_of_day_review_dedupe_key(actor_id: str, today: date) -> str:
    return f"slack-end-of-day-review/{actor_id}/{today.isoformat()}"


def _proposal_lines(
    proposals: list[Proposal],
    *,
    empty: str,
    all_proposals: list[Proposal] | None = None,
    today: date | None = None,
) -> list[str]:
    if not proposals:
        return [empty]
    visible_ids = {proposal.proposal_id for proposal in proposals}
    all_items = all_proposals or proposals
    all_by_id = {proposal.proposal_id: proposal for proposal in all_items}
    lines: list[str] = []
    seen: set[str] = set()
    sort_key = (
        (lambda item: proposal_deadline_sort_key(item, today=today, overdue_last=True))
        if today is not None
        else _sort_key
    )
    for proposal in sorted(proposals, key=sort_key):
        anchor = _section_anchor(proposal, all_by_id, visible_ids)
        if anchor.proposal_id in seen:
            continue
        seen.add(anchor.proposal_id)
        lines.append(_proposal_line(anchor, today=today))
        all_children = child_proposals(anchor, all_items)
        visible_children = _display_children(all_children, max_completed_children=2)
        if all_children:
            lines.append(_subtask_summary_line(all_children, visible_children))
        for index, child in enumerate(visible_children):
            connector = "└─" if index == len(visible_children) - 1 else "├─"
            lines.append(f"    {connector} {_subtask_line(child, today=today)}")
    return lines


def _proposal_line(proposal: Proposal, *, today: date | None) -> str:
    if _is_workflow_context_parent(proposal):
        label = _title_display(proposal)
        return f"- {label} — 하위작업 중심으로 확인합니다."
    return f"- {render_confirmed_sentence(proposal)}{_overdue_label(proposal, today=today)}"


def _subtask_summary_line(all_children: tuple[Proposal, ...], visible_children: tuple[Proposal, ...]) -> str:
    done_count = sum(1 for child in all_children if child.status in {"done", "applied"})
    total = len(all_children)
    hidden_count = sum(1 for child in all_children if child.status in {"done", "applied"}) - sum(
        1 for child in visible_children if child.status in {"done", "applied"}
    )
    next_child = next((child for child in all_children if not is_relation_complete(child)), None)
    next_label = ""
    if next_child is not None:
        step = step_label(next_child)
        next_label = f" · 다음: {step} {next_child.title}" if step else f" · 다음: {next_child.title}"
    hidden_label = f" · 완료 {hidden_count}개 숨김" if hidden_count else ""
    return f"  ↳ 하위작업 {done_count}/{total} 완료{next_label}{hidden_label}"


def _subtask_line(proposal: Proposal, *, today: date | None) -> str:
    step = step_label(proposal)
    step_prefix = f"[{step}] " if step else ""
    when = human_when_label(proposal)
    when_part = ""
    if when:
        when_kind = "일정" if proposal.kind == "event" or proposal.scheduled_date is not None else "마감"
        when_part = f"{when_kind} {when}"
    parts = [
        _status_display(proposal.status),
        when_part,
        f"담당 {actor_label(proposal.assigned_to)}",
    ]
    detail = " · ".join(part for part in parts if part)
    return (
        f"{step_prefix}{_title_display(proposal)}"
        + (f" — {detail}" if detail else "")
        + f" `{short_id(proposal.proposal_id)}`"
        + _overdue_label(proposal, today=today)
    )


def _title_display(proposal: Proposal) -> str:
    if proposal.status in {"done", "applied"}:
        return f"~{proposal.title}~"
    return proposal.title


def _section_anchor(proposal: Proposal, proposals_by_id: dict[str, Proposal], visible_ids: set[str]) -> Proposal:
    current = proposal
    anchor: Proposal | None = None
    seen: set[str] = set()
    while True:
        parent_id = parent_proposal_id(current)
        parent = proposals_by_id.get(parent_id) if parent_id else None
        if parent is None or parent.proposal_id in seen:
            break
        seen.add(parent.proposal_id)
        if parent.proposal_id in visible_ids or _is_workflow_context_parent(parent):
            anchor = parent
        current = parent
    return anchor or proposal


def _display_children(children: tuple[Proposal, ...], *, max_completed_children: int) -> tuple[Proposal, ...]:
    completed = [child for child in children if child.status in {"done", "applied"}]
    if len(completed) <= max_completed_children:
        return children
    recent_completed_ids = {
        child.proposal_id
        for child in sorted(completed, key=_completion_sort_key, reverse=True)[:max_completed_children]
    }
    return tuple(
        child
        for child in children
        if child.status not in {"done", "applied"} or child.proposal_id in recent_completed_ids
    )


def _completion_sort_key(proposal: Proposal) -> tuple[str, str]:
    completed_at = proposal.metadata.get("completed_at", "")
    updated_at = proposal.updated_at.isoformat(timespec="seconds") if proposal.updated_at else ""
    created_at = proposal.created_at.isoformat(timespec="seconds") if proposal.created_at else ""
    return (completed_at or updated_at or created_at, proposal.proposal_id)


def _is_workflow_context_parent(proposal: Proposal) -> bool:
    return proposal.metadata.get("workflow_container") == "true" or proposal.metadata.get(WORKFLOW_ROLE_KEY) == WORKFLOW_PARENT_ROLE


def _status_display(status: str) -> str:
    icon = proposal_status_icon(status)
    label = proposal_status_label(status)
    return f"{icon} {label}" if icon else label


def _attention_lines(items: tuple[HumanAttentionItem, ...], *, today: date) -> list[str]:
    lines: list[str] = []
    for item in sorted(items, key=lambda attention: _attention_display_sort_key(attention, today=today)):
        proposal = item.proposal
        if item.kind in {"missing_info", "deferred_reminder"}:
            question_proposal = _proposal_with_deferred_missing_slots(proposal)
            reminder_prefix = "다시 확인할 항목입니다. " if item.kind == "deferred_reminder" else ""
            lines.append(
                "- "
                + reminder_prefix
                + render_missing_slot_sentence(
                    build_missing_slot_question(
                        question_proposal,
                        request_id=item.request.request_id if item.request else "",
                    )
                )
            )
            continue
        if item.kind == "approval_decision":
            lines.append(f"- *{proposal.title}*: 승인/변경/거절 중 어떻게 처리할지 알려주세요.")
            continue

        due_detail = _proposal_due_detail(proposal, today=today)
        urgency = work_item_urgency_label(proposal, today=today)
        overdue_label = f" · 🔴 {urgency}" if urgency else ""
        if item.blocking_dependencies:
            blockers = ", ".join(f"*{dependency.title}*" for dependency in item.blocking_dependencies)
            lines.append(
                f"- *{proposal.title}*: {due_detail}{overdue_label}. 먼저 {blockers} 완료 여부가 확인되어야 합니다."
                " 선행 작업의 완료/진행/연기 상태를 알려주세요."
            )
            continue
        if is_past_scheduled_commitment(proposal, today=today):
            prefix = "일정이 지난 항목입니다"
        elif item.overdue:
            prefix = "마감이 지난 작업입니다"
        else:
            prefix = "오늘 확인할 작업입니다"
        lines.append(
            f"- *{proposal.title}*: {due_detail}{overdue_label}. {prefix}. "
            "완료/진행/연기 중 하나로 알려주세요."
        )
    return lines or ["- 지금 바로 답장/확인할 항목은 없습니다."]


def _overdue_label(proposal: Proposal, *, today: date | None) -> str:
    if today is None:
        return ""
    urgency = work_item_urgency_label(proposal, today=today)
    return f" · 🔴 {urgency}" if urgency else ""


def _is_overdue(proposal: Proposal, *, today: date) -> bool:
    return needs_time_resolution(proposal, today=today)


def _attention_display_sort_key(item: HumanAttentionItem, *, today: date) -> tuple[int, str, int, str, str]:
    kind_rank = {
        "progress_confirmation": "0",
        "deferred_reminder": "1",
        "missing_info": "2",
        "approval_decision": "3",
    }[item.kind]
    deadline_key = proposal_deadline_sort_key(item.proposal, today=today, overdue_last=True)
    return (*deadline_key, kind_rank)


def _proposal_with_deferred_missing_slots(proposal: Proposal) -> Proposal:
    if proposal.missing_slots:
        return proposal
    slots = deferred_missing_slots(proposal)
    if not slots:
        return proposal
    return replace(proposal, missing_slots=slots)


def _proposal_due_detail(proposal: Proposal, *, today: date) -> str:
    proposal_date = proposal.due_date or proposal.scheduled_date
    if proposal_date is None:
        return "기한 미정"
    parts = [date_label(proposal_date)]
    if proposal.time_window:
        parts.append(proposal.time_window)
    return f"{work_item_due_detail_label(proposal, today=today)}: " + " ".join(parts)


def _proposal_date(proposal: Proposal) -> date | None:
    return proposal.scheduled_date or proposal.due_date


def _sort_key(proposal: Proposal) -> tuple[str, str, str]:
    proposal_date = _proposal_date(proposal)
    return (
        proposal_date.isoformat() if proposal_date else "9999-12-31",
        proposal.time_window,
        proposal.title,
    )


def _is_personal_scope(proposal: Proposal, actor_id: str) -> bool:
    participants = {item.strip() for item in proposal.metadata.get("participants", "").split(",") if item.strip()}
    return (
        actor_id
        in {
            proposal.assigned_to,
            proposal.proposer_id,
            *proposal.required_approvers,
            *proposal.approvals,
            *participants,
        }
        or proposal.assigned_to in {"shared", "unassigned"}
    )


def _completed_on(proposal: Proposal, *, today: date) -> bool:
    completed_at = proposal.metadata.get("completed_at", "")
    if completed_at:
        try:
            return datetime.fromisoformat(completed_at).date() == today
        except ValueError:
            return False
    return _proposal_date(proposal) == today


def _truncate(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 60].rstrip() + "\n…\n(길어서 일부만 표시했습니다.)"
