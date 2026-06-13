from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .attention import (
    HumanAttentionItem,
    collect_human_attention_items,
    deferred_missing_slots,
    has_deferred_missing_info,
)
from .domain import KIND_SPECS, OutboundMessage, Proposal
from .outbound_delivery import reserve_outbound
from .hierarchy_view import (
    display_children,
    is_workflow_context_parent,
    section_anchor,
)
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
    COMPLETED_AT_KEY,
    LOCATION_KEY,
    blocking_dependencies,
    child_proposals,
    is_relation_complete,
    step_label,
)
from .sort_keys import proposal_deadline_sort_key, schedule_first_sort_key
from .store import TeamTaskStore
from .work_item_state import (
    is_past_scheduled_commitment,
    is_personal_scope,
    needs_time_resolution,
    requires_progress_confirmation,
    schedule_first_date,
    work_item_due_detail_label,
    work_item_urgency_label,
)


@dataclass(frozen=True)
class _ProactiveBody:
    """One surface instance ready for the shared reserve/emit lifecycle.

    ``build`` callables return zero or more of these.  A single briefing yields
    one body; ``proactive_checks`` yields one per due proposal.  Each body owns
    everything that differs per emission: the rendered text, card payload, the
    optional ``proposal_id`` carried on the outbound message, the per-emission
    ``dedupe_key``, and the ``event_payload`` recorded on reserve.
    """

    text: str
    card: dict[str, str]
    dedupe_key: str
    event_payload: dict[str, Any]
    proposal_id: str = ""


@dataclass(frozen=True)
class ProactiveSurface:
    """One proactive-secretary surface in declarative form.

    The four historical builders repeated the same preflight ->
    ``reserve_outbound`` -> ``_truncate`` lifecycle, differing only in the
    per-surface dedupe key, ``provider_message_id`` suffix, ``event_type``, and
    Korean body.  A surface captures those differences so adding a new proactive
    surface is one ``ProactiveSurface`` entry plus its ``build`` function.

    INVARIANT 7 (outbound dedup) depends on ``dedupe_key`` producing
    byte-identical strings and ``emit_proactive`` recording the same delivery and
    event as the inlined copies it replaces, so no key/suffix/event may change.
    """

    name: str
    message_type: str
    event_type: str
    provider_message_id: str
    dedupe_key: Callable[..., str]
    build: Callable[..., tuple[_ProactiveBody, ...]]
    truncate: bool = True


def emit_proactive(
    store: TeamTaskStore,
    surface: ProactiveSurface,
    bodies: tuple[_ProactiveBody, ...],
    *,
    now: datetime,
    actor_id: str,
    reserve: bool,
    max_chars: int = 3900,
) -> tuple[OutboundMessage, ...]:
    """Shared reserve/emit flow for every proactive surface.

    Renders each body's text (truncating when the surface opts in), builds the
    ``OutboundMessage`` with the surface's ``message_type``, and reserves the
    single outbound delivery via ``reserve_outbound`` using the surface's
    ``provider_message_id`` suffix and ``event_type``.  Bodies whose dedupe key is
    already reserved are skipped, mirroring the historical per-builder behavior.
    """

    messages: list[OutboundMessage] = []
    for body in bodies:
        text = _truncate(body.text, max_chars=max_chars) if surface.truncate else body.text
        message = OutboundMessage(
            surface="personal_chat",
            recipient_id=actor_id,
            message_type=surface.message_type,
            text=text,
            proposal_id=body.proposal_id,
            card=body.card,
        )
        if reserve and not reserve_outbound(
            store,
            message,
            dedupe_key=body.dedupe_key,
            provider_message_id=surface.provider_message_id,
            event_type=surface.event_type,
            event_payload=body.event_payload,
            now=now,
        ):
            continue
        messages.append(message)
    return tuple(messages)


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
    body = _ProactiveBody(
        text="\n".join(lines).strip(),
        card={
            "dedupe_key": dedupe_key,
            "date": today.isoformat(),
            "confirmed_count": str(len(today_confirmed)),
            "due_task_count": str(len(due_tasks)),
            "pending_count": str(len(pending)),
            "attention_count": str(len(attention_items)),
            "deferred_due_count": str(len(deferred_due)),
        },
        dedupe_key=dedupe_key,
        event_payload={"dedupe_key": dedupe_key, "actor_id": actor_id, "date": today.isoformat()},
    )
    return emit_proactive(
        store,
        MORNING_BRIEFING_SURFACE,
        (body,),
        now=now,
        actor_id=actor_id,
        reserve=reserve,
        max_chars=max_chars,
    )


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
    body = _ProactiveBody(
        text="\n".join(lines).strip(),
        card={
            "dedupe_key": dedupe_key,
            "date": today.isoformat(),
            "today_open_count": str(len(today_open)),
            "week_open_count": str(len(week_open)),
            "attention_count": str(len(attention_items)),
        },
        dedupe_key=dedupe_key,
        event_payload={"dedupe_key": dedupe_key, "actor_id": actor_id, "date": today.isoformat()},
    )
    return emit_proactive(
        store,
        AFTERNOON_BRIEFING_SURFACE,
        (body,),
        now=now,
        actor_id=actor_id,
        reserve=reserve,
        max_chars=max_chars,
    )


def build_proactive_checks(
    store: TeamTaskStore,
    *,
    now: datetime,
    actor_id: str = "me",
    reserve: bool = True,
) -> tuple[OutboundMessage, ...]:
    """Ask whether due unfinished work has been done, with per-day dedupe."""

    today = now.date()
    bodies: list[_ProactiveBody] = []
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
        bodies.append(
            _ProactiveBody(
                text=text,
                card={
                    "dedupe_key": dedupe_key,
                    "proposal_id": proposal.proposal_id,
                    "title": proposal.title,
                    "due_date": proposal.due_date.isoformat() if proposal.due_date else "",
                    "scheduled_date": proposal.scheduled_date.isoformat() if proposal.scheduled_date else "",
                    "overdue": str(overdue).lower(),
                },
                dedupe_key=dedupe_key,
                event_payload={
                    "dedupe_key": dedupe_key,
                    "proposal_id": proposal.proposal_id,
                    "actor_id": actor_id,
                    "date": today.isoformat(),
                },
                proposal_id=proposal.proposal_id,
            )
        )
    return emit_proactive(
        store,
        PROACTIVE_CHECK_SURFACE,
        tuple(bodies),
        now=now,
        actor_id=actor_id,
        reserve=reserve,
    )


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
        and KIND_SPECS[proposal.kind].schedulable
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
            detail = " · ".join(item for item in (due_label, proposal.time_window, proposal.metadata.get(LOCATION_KEY, "")) if item)
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
    body = _ProactiveBody(
        text="\n".join(lines).strip(),
        card={
            "dedupe_key": dedupe_key,
            "date": today.isoformat(),
            "review_count": str(len(review_items)),
            "pending_count": str(len(pending_items)),
            "completed_today_count": str(len(completed_today)),
        },
        dedupe_key=dedupe_key,
        event_payload={"dedupe_key": dedupe_key, "actor_id": actor_id, "date": today.isoformat()},
    )
    return emit_proactive(
        store,
        END_OF_DAY_REVIEW_SURFACE,
        (body,),
        now=now,
        actor_id=actor_id,
        reserve=reserve,
        max_chars=max_chars,
    )


def morning_briefing_dedupe_key(actor_id: str, today: date) -> str:
    return f"slack-morning-briefing/{actor_id}/{today.isoformat()}"


def afternoon_briefing_dedupe_key(actor_id: str, today: date) -> str:
    return f"slack-afternoon-briefing/{actor_id}/{today.isoformat()}"


def proactive_check_dedupe_key(proposal: Proposal, *, actor_id: str, today: date) -> str:
    return f"slack-proactive-check/{actor_id}/{proposal.proposal_id}/{today.isoformat()}"


def end_of_day_review_dedupe_key(actor_id: str, today: date) -> str:
    return f"slack-end-of-day-review/{actor_id}/{today.isoformat()}"


MORNING_BRIEFING_SURFACE = ProactiveSurface(
    name="morning_briefing",
    message_type="morning_briefing",
    event_type="briefing.morning.created",
    provider_message_id="morning-briefing-preview",
    dedupe_key=morning_briefing_dedupe_key,
    build=build_morning_briefing,
)

AFTERNOON_BRIEFING_SURFACE = ProactiveSurface(
    name="afternoon_briefing",
    message_type="afternoon_briefing",
    event_type="briefing.afternoon.created",
    provider_message_id="afternoon-briefing-preview",
    dedupe_key=afternoon_briefing_dedupe_key,
    build=build_afternoon_briefing,
)

PROACTIVE_CHECK_SURFACE = ProactiveSurface(
    name="proactive_check",
    message_type="due_work_check",
    event_type="proactive_check.created",
    provider_message_id="proactive-check-preview",
    dedupe_key=proactive_check_dedupe_key,
    build=build_proactive_checks,
    truncate=False,
)

END_OF_DAY_REVIEW_SURFACE = ProactiveSurface(
    name="end_of_day_review",
    message_type="end_of_day_review",
    event_type="briefing.end_of_day.created",
    provider_message_id="end-of-day-review-preview",
    dedupe_key=end_of_day_review_dedupe_key,
    build=build_end_of_day_review,
)

PROACTIVE_SURFACES: tuple[ProactiveSurface, ...] = (
    MORNING_BRIEFING_SURFACE,
    AFTERNOON_BRIEFING_SURFACE,
    PROACTIVE_CHECK_SURFACE,
    END_OF_DAY_REVIEW_SURFACE,
)


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
        anchor = section_anchor(proposal, all_by_id, visible_ids)
        if anchor.proposal_id in seen:
            continue
        seen.add(anchor.proposal_id)
        lines.append(_proposal_line(anchor, today=today))
        all_children = child_proposals(anchor, all_items)
        visible_children = display_children(all_children, max_completed_children=2)
        if all_children:
            lines.append(_subtask_summary_line(all_children, visible_children))
        for index, child in enumerate(visible_children):
            connector = "└─" if index == len(visible_children) - 1 else "├─"
            lines.append(f"    {connector} {_subtask_line(child, today=today)}")
    return lines


def _proposal_line(proposal: Proposal, *, today: date | None) -> str:
    if is_workflow_context_parent(proposal):
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
        when_kind = "일정" if KIND_SPECS[proposal.kind].schedulable or proposal.scheduled_date is not None else "마감"
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


_proposal_date = schedule_first_date
_sort_key = schedule_first_sort_key
_is_personal_scope = is_personal_scope


def _completed_on(proposal: Proposal, *, today: date) -> bool:
    completed_at = proposal.metadata.get(COMPLETED_AT_KEY, "")
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
