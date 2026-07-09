from __future__ import annotations

from datetime import date, timedelta
from html import escape
from typing import Any

from .attention import has_deferred_missing_info
from .domain import KIND_SPECS, ApprovalRequest, OutboundMessage, Proposal
from .hierarchy_view import (
    display_children,
    is_workflow_context_parent,
    section_anchor,
)
from .human_view import (
    date_label,
    missing_slots_card_label,
    proposal_date_window_label,
    proposal_participants_label,
    proposal_status_label,
)
from .relations import (
    CONFLICT_DETECTED_KEY,
    CONFLICT_WITH_PROPOSAL_IDS_KEY,
    DATE_WINDOW_KIND_KEY,
    DEFERRED_UNTIL_KEY,
    LINK_PREP_SUBTASK,
    LINK_TYPE_KEY,
    LOCATION_KEY,
    PARTICIPANTS_KEY,
    PROGRESS_NOTE_KEY,
    PROGRESS_STATUS_KEY,
    PROGRESS_UPDATED_AT_KEY,
    REMAINING_WORK_KEY,
    child_proposals,
    is_workflow_parent,
    step_label,
    workflow_projection,
)
from .sort_keys import proposal_deadline_sort_key
from .store import TeamTaskStore
from .timeline import proposal_timeline
from .work_item_state import (
    is_open_work_item,
    is_surface_visible_item,
    needs_time_resolution,
    work_item_urgency_label,
)


SURFACE_ROLES: dict[str, dict[str, str]] = {
    "personal_chat": {
        "label": "개인 DM (Slack dogfood)",
        "primary_role": "빠른 입력, 담당자 확인, 미해결 질문, 개인 리마인더",
        "main_when": "지금은 Slack 나에게 DM으로 받고 되묻는 1인 dogfood 루프",
    },
    "team_room": {
        "label": "팀 공유방 (future)",
        "primary_role": "확정 결과, 공동 질문, 팀 전체 리마인더",
        "main_when": "Kakao/Slack 팀방 실제 연결 전에는 simulator-only",
    },
    "web_task_page": {
        "label": "웹 task page",
        "primary_role": "floating·질문·확정·루틴·리마인더·preview 상태 정리",
        "main_when": "여러 항목을 한 번에 보고 운영 상태를 점검할 때",
    },
}


BOARD_LABELS = {
    "today": "오늘",
    "this_week": "이번 주",
    "pending_approvals": "승인 대기",
    "questions": "미해결 질문",
    "floating": "떠 있는 항목",
    "routines": "반복 루틴",
    "prep_subtasks": "준비 작업",
    "reminders": "리마인더",
    "references": "참고 링크",
    "by_assignee": "담당자별",
    "done": "완료",
}


PREVIEW_LABELS = {
    "ready": "export 후보",
    "applied": "적용됨",
    "blocked": "확인 필요",
    "excluded": "preview 제외",
}

PREVIEW_PILL_CLASS = {
    "ready": "ok",
    "applied": "ok",
    "blocked": "danger",
    "excluded": "",
}


def render_kakao_text_card(message: OutboundMessage) -> str:
    """Render a durable text-first card suitable for KakaoTalk-like chat surfaces."""

    if message.surface == "personal_chat":
        header = "[승인 요청]" if message.message_type == "approval_request" else "[개인 알림]"
    elif message.surface == "team_room":
        header = "[팀 공유]"
    else:
        header = "[웹 업데이트]"

    lines = [header, message.text]
    if message.card:
        lines.extend(
            [
                "",
                f"- 제목: {message.card.get('title', '')}",
                f"- 종류: {message.card.get('kind', '')}",
                f"- 담당: {message.card.get('assigned_to', '')}",
                f"- 영역: {message.card.get('task_management_area', '')}",
            ]
        )
        if message.card.get("date"):
            lines.append(f"- 날짜: {message.card['date']}")
        if message.card.get("date_window"):
            lines.append(f"- 가능 기간: {message.card['date_window']}")
        if message.card.get("missing_slots"):
            lines.append(f"- 확인 필요: {_card_missing_slots_label(message.card)}")
    if message.approval_request_id:
        lines.extend(
            [
                "",
                "답장:",
                f"수락 {message.approval_request_id}",
                f"거절 {message.approval_request_id}",
                f"변경 {message.approval_request_id} <새 내용>",
            ]
        )
    return "\n".join(lines).strip()


def _card_missing_slots_label(card: dict[str, str]) -> str:
    return missing_slots_card_label(
        card.get("missing_slot_labels", ""),
        card.get("missing_slots", ""),
    )


def build_web_task_page_model(
    store: TeamTaskStore,
    *,
    today: date,
    max_completed_children: int | None = None,
) -> dict[str, Any]:
    proposals = store.list_proposals()
    pending_requests = store.list_approval_requests(status="pending")
    events = store.read_events()
    applied_exports = store.list_applied_exports()
    week_end = today + timedelta(days=6)
    surface_proposals = tuple(item for item in proposals if is_surface_visible_item(item))
    sorted_proposals = sorted(
        surface_proposals,
        key=lambda item: proposal_deadline_sort_key(item, today=today, overdue_last=True),
    )
    proposal_views = {
        item.proposal_id: _proposal_view(item, applied_exports.get(item.proposal_id), today=today, events=events, proposals=surface_proposals)
        for item in sorted_proposals
    }

    sections = {
        "today": _hierarchy_section(
            [item for item in sorted_proposals if _belongs_in_today_section(item, today=today)],
            proposals=surface_proposals,
            proposal_views=proposal_views,
            max_completed_children=max_completed_children,
        ),
        "this_week": _hierarchy_section(
            [
                item
                for item in sorted_proposals
                if _is_open_status(item)
                and (proposal_date := _proposal_date(item)) is not None
                and today <= proposal_date <= week_end
            ],
            proposals=surface_proposals,
            proposal_views=proposal_views,
            max_completed_children=max_completed_children,
        ),
        "pending_approvals": [
            _request_view(request, proposals)
            for request in pending_requests
            if _request_surface_visible(request, proposals)
        ],
        "questions": _hierarchy_section([item for item in sorted_proposals if KIND_SPECS[item.kind].dashboard_section == "questions"], proposals=surface_proposals, proposal_views=proposal_views, max_completed_children=max_completed_children),
        "floating": _hierarchy_section(
            [item for item in sorted_proposals if item.status in {"draft", "posted", "awaiting_approval"} or item.missing_slots],
            proposals=surface_proposals,
            proposal_views=proposal_views,
            max_completed_children=max_completed_children,
        ),
        "routines": _hierarchy_section([item for item in sorted_proposals if KIND_SPECS[item.kind].dashboard_section == "routines"], proposals=surface_proposals, proposal_views=proposal_views, max_completed_children=max_completed_children),
        "prep_subtasks": _hierarchy_section(
            [item for item in sorted_proposals if item.metadata.get(LINK_TYPE_KEY) == LINK_PREP_SUBTASK],
            proposals=surface_proposals,
            proposal_views=proposal_views,
            max_completed_children=max_completed_children,
        ),
        "reminders": [
            {
                "type": event.get("type", ""),
                "occurred_at": event.get("occurred_at", ""),
                "proposal_id": event.get("payload", {}).get("proposal_id", ""),
                "dedupe_key": event.get("payload", {}).get("dedupe_key", ""),
            }
            for event in events
            if str(event.get("type", "")).startswith("reminder.")
        ],
        "references": _hierarchy_section([item for item in sorted_proposals if KIND_SPECS[item.kind].dashboard_section == "references"], proposals=surface_proposals, proposal_views=proposal_views, max_completed_children=max_completed_children),
        "by_assignee": _by_assignee(surface_proposals, applied_exports, today=today),
        "done": _hierarchy_section(
            [item for item in sorted_proposals if item.status in {"done", "applied"}],
            proposals=surface_proposals,
            proposal_views=proposal_views,
            max_completed_children=max_completed_children,
        ),
    }
    status_counts = _status_counts(proposals)
    preview_counts = _preview_counts(proposals, applied_exports)

    return {
        "surface_roles": SURFACE_ROLES,
        "today": today.isoformat(),
        "today_label": date_label(today),
        "counts": {
            "total": len(proposals),
            "approved": len([item for item in proposals if item.status == "approved"]),
            "awaiting_approval": len([item for item in proposals if item.status == "awaiting_approval"]),
            "questions": len(sections["questions"]),
            "floating": len(sections["floating"]),
            "routines": len(sections["routines"]),
            "prep_subtasks": len(sections["prep_subtasks"]),
            "reminders": len(sections["reminders"]),
            "references": len(sections["references"]),
            "done": len([item for item in proposals if item.status in {"done", "applied"}]),
            "preview_ready": preview_counts["ready"],
            "preview_blocked": preview_counts["blocked"],
            "preview_applied": preview_counts["applied"],
        },
        "sections": sections,
        "nav_counts": {
            "today": len(sections["today"]),
            "this_week": len(sections["this_week"]),
            "pending_approvals": len(sections["pending_approvals"]),
            "questions": len(sections["questions"]),
            "floating": len(sections["floating"]),
            "routines": len(sections["routines"]),
            "prep_subtasks": len(sections["prep_subtasks"]),
            "reminders": len(sections["reminders"]),
            "references": len(sections["references"]),
            "by_assignee": len(sections["by_assignee"]),
            "done": len(sections["done"]),
        },
        "status_counts": status_counts,
        "preview_counts": preview_counts,
        "recent_events": tuple(events[-8:]),
        "event_log": events,
    }


def render_web_task_page_html(model: dict[str, Any]) -> str:
    """Render a static local dashboard page using task-core workbench styling."""

    counts = model["counts"]
    sections = model["sections"]
    nav_counts = model.get("nav_counts", {})
    preview_counts = model.get("preview_counts", {})
    event_count = len(model.get("event_log", []))
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TeamTask Task Page</title>
  <style>
    :root {{
      --brand-bg: #fafafa;
      --brand-surface: #ffffff;
      --brand-surface-muted: #f8fafc;
      --brand-hover: #f2f4f7;
      --brand-border: #e4e7ec;
      --brand-border-strong: #d0d5dd;
      --brand-text: #1d2939;
      --brand-text-soft: #475467;
      --brand-text-mute: #667085;
      --brand-text-faint: #98a2b3;
      --brand-accent: #0e7490;
      --brand-accent-strong: #155e75;
      --brand-accent-soft: #ecfeff;
      --brand-amber: #b45309;
      --brand-amber-soft: #fef3c7;
      --brand-danger: #b42318;
      --brand-danger-soft: #fee4e2;
      --brand-ok: #067647;
      --brand-ok-soft: #ecfdf3;
      --brand-neutral-soft: #f0f4f8;
      --brand-radius-sm: 4px;
      --brand-radius: 6px;
      --brand-radius-md: 8px;
      --brand-radius-lg: 10px;
      --brand-radius-pill: 16px;
      --brand-shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.06);
      --brand-space-1: 4px;
      --brand-space-2: 8px;
      --brand-space-3: 12px;
      --brand-space-4: 16px;
      --brand-space-5: 20px;
      --brand-space-6: 24px;
      --brand-space-7: 32px;
      --brand-space-8: 48px;
      --brand-font-sans: "NanumGothic", "Pretendard Variable", Pretendard, -apple-system, BlinkMacSystemFont, system-ui, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
      --brand-font-mono: ui-monospace, SFMono-Regular, Menlo, "Cascadia Code", "JetBrains Mono", Consolas, monospace;
      --font-body: 400 15px/1.65 var(--brand-font-sans);
      --font-body-tight: 400 14px/1.55 var(--brand-font-sans);
      --font-small: 400 13px/1.55 var(--brand-font-sans);
      --font-h1: 700 2rem/1.2 var(--brand-font-sans);
      --font-h2: 600 1.2rem/1.35 var(--brand-font-sans);
      --font-h3: 600 1rem/1.4 var(--brand-font-sans);
      --font-h4: 600 0.72rem/1.4 var(--brand-font-sans);
      --font-eyebrow: 600 0.72rem/1.4 var(--brand-font-mono);
      --font-meta: 400 0.82rem/1.5 var(--brand-font-mono);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--brand-bg);
      color: var(--brand-text);
      font: var(--font-body);
      letter-spacing: 0;
    }}
    html {{ scroll-behavior: smooth; }}
    a {{ color: inherit; text-decoration: none; }}
    a:focus-visible,
    [role="button"]:focus-visible {{
      outline: 2px solid var(--brand-accent);
      outline-offset: 2px;
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 10;
      height: 56px;
      display: flex;
      align-items: center;
      gap: var(--brand-space-5);
      padding: 0 var(--brand-space-6);
      background: var(--brand-surface);
      border-bottom: 1px solid var(--brand-border);
    }}
    .brand {{
      display: flex;
      align-items: baseline;
      gap: 10px;
      min-width: max-content;
      font-weight: 600;
    }}
    .brand .inst {{
      color: var(--brand-text-mute);
      font: var(--font-eyebrow);
      text-transform: uppercase;
    }}
    .brand .lead {{ color: var(--brand-accent-strong); }}
    .topnav {{
      display: flex;
      gap: 2px;
      min-width: 0;
      overflow-x: auto;
    }}
    .topnav a {{
      flex: 0 0 auto;
      color: var(--brand-text-soft);
      padding: 6px 12px;
      border-radius: var(--brand-radius);
      font: var(--font-body-tight);
      white-space: nowrap;
    }}
    .topnav a:hover,
    .topnav a.active {{
      color: var(--brand-accent-strong);
      background: var(--brand-accent-soft);
      font-weight: 600;
    }}
    .topbar-stat {{
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: max-content;
      color: var(--brand-text-mute);
      font: var(--font-meta);
    }}
    .topbar-stat .dot {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--brand-ok);
    }}
    .app {{
      display: grid;
      grid-template-columns: 248px minmax(0, 1fr);
      max-width: 1480px;
      min-height: calc(100vh - 56px);
      margin: 0 auto;
    }}
    .sidebar {{
      position: sticky;
      top: 56px;
      height: calc(100vh - 56px);
      padding: 18px 14px 24px;
      background: var(--brand-surface);
      border-right: 1px solid var(--brand-border);
      overflow-y: auto;
    }}
    .side-group {{ margin-bottom: 22px; }}
    .side-h {{
      padding: 0 10px 8px;
      color: var(--brand-text-mute);
      font: var(--font-h4);
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    .side-link {{
      display: flex;
      align-items: center;
      gap: 10px;
      min-height: 32px;
      padding: 6px 10px;
      border-radius: var(--brand-radius);
      color: var(--brand-text-soft);
      font: var(--font-body-tight);
      cursor: pointer;
    }}
    .side-link:hover {{
      background: var(--brand-hover);
      color: var(--brand-text);
    }}
    .side-link.active {{
      background: var(--brand-accent-soft);
      color: var(--brand-accent-strong);
      font-weight: 600;
    }}
    .side-link .count {{
      margin-left: auto;
      color: var(--brand-text-mute);
      font-team: var(--brand-font-mono);
      font-size: 0.74rem;
    }}
    .surface-note {{
      margin: 16px 6px 0;
      padding: 8px;
      border: 1px solid rgba(180, 83, 9, 0.28);
      border-radius: var(--brand-radius-sm);
      background: var(--brand-amber-soft);
      color: var(--brand-amber);
      font: 400 0.72rem/1.5 var(--brand-font-mono);
    }}
    main.board {{
      min-width: 0;
      padding: 36px 46px 96px;
    }}
    .board-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: var(--brand-space-7);
      align-items: start;
    }}
    .board-eyebrow {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 6px;
      color: var(--brand-accent-strong);
      font: var(--font-eyebrow);
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    .board-title {{
      margin: 0 0 6px;
      color: var(--brand-text);
      font: var(--font-h1);
      letter-spacing: 0;
    }}
    .board-sub {{
      max-width: 780px;
      color: var(--brand-text-mute);
      font: var(--font-body-tight);
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(108px, 1fr));
      gap: var(--brand-space-2);
      margin: 22px 0 10px;
    }}
    .metric {{
      display: grid;
      gap: 2px;
      padding: 12px;
      border: 1px solid var(--brand-border);
      border-radius: var(--brand-radius);
      background: var(--brand-surface);
      box-shadow: var(--brand-shadow-sm);
    }}
    .metric strong {{
      color: var(--brand-text);
      font: 700 1.45rem/1.1 var(--brand-font-mono);
    }}
    .metric span {{
      color: var(--brand-text-mute);
      font: var(--font-small);
    }}
    .ops-strip {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: var(--brand-space-2);
      margin: 14px 0 6px;
    }}
    .ops-card {{
      padding: 12px;
      border: 1px solid var(--brand-border);
      border-radius: var(--brand-radius);
      background: var(--brand-surface-muted);
      color: var(--brand-text-soft);
      font: var(--font-small);
    }}
    .ops-card strong {{
      display: block;
      color: var(--brand-text);
      font: var(--font-h3);
    }}
    .section {{
      margin-top: 28px;
      scroll-margin-top: 72px;
    }}
    .section:target,
    .panel:target {{
      animation: target-pulse 1.1s ease-out;
    }}
    @keyframes target-pulse {{
      0% {{ box-shadow: 0 0 0 4px rgba(14, 116, 144, 0.18); }}
      100% {{ box-shadow: none; }}
    }}
    .section-head {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 0;
      border-bottom: 1px solid var(--brand-border);
    }}
    .section-head .title {{
      color: var(--brand-text);
      font: var(--font-h3);
    }}
    .section-head .count {{
      color: var(--brand-text-mute);
      font: var(--font-meta);
    }}
    .task-row {{
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr) auto;
      gap: 10px;
      padding: 12px 8px 12px 0;
      border-bottom: 1px solid #eef1f5;
      border-radius: var(--brand-radius-sm);
      cursor: pointer;
    }}
    .task-row:hover {{
      background: var(--brand-surface-muted);
    }}
    .task-row.is-open {{
      background: var(--brand-surface);
      box-shadow: inset 3px 0 0 var(--brand-accent);
    }}
    .prio-check {{
      width: 20px;
      height: 20px;
      margin-top: 2px;
      border: 1.5px solid var(--priority-color, var(--brand-text-faint));
      border-radius: 50%;
      background: var(--brand-surface);
    }}
    .task-row[data-status="approved"],
    .task-row[data-status="applied"] {{ --priority-color: var(--brand-ok); }}
    .task-row[data-status="awaiting_approval"] {{ --priority-color: var(--brand-accent); }}
    .task-row[data-status="rejected"] {{ --priority-color: var(--brand-danger); }}
    .task-row[data-status="done"] {{ --priority-color: var(--brand-text-faint); opacity: 0.68; }}
    .task-title {{
      color: var(--brand-text);
      font-size: 0.96rem;
      line-height: 1.45;
      word-break: keep-all;
      overflow-wrap: anywhere;
    }}
    .task-row[data-status="done"] .task-title {{
      color: var(--brand-text-mute);
      text-decoration: line-through;
      text-decoration-color: var(--brand-text-faint);
    }}
    .task-children {{
      position: relative;
      margin: 10px 0 4px 34px;
      padding: 18px 0 4px 18px;
      border-left: 3px solid rgba(14, 116, 144, 0.34);
      border-radius: 0 0 0 var(--brand-radius);
      background: linear-gradient(90deg, rgba(14, 116, 144, 0.06), rgba(255, 255, 255, 0));
    }}
    .task-children-label {{
      position: absolute;
      top: -9px;
      left: -3px;
      padding: 2px 8px;
      border: 1px solid rgba(14, 116, 144, 0.22);
      border-radius: 999px;
      background: var(--brand-surface);
      color: var(--brand-accent-strong);
      font: var(--font-meta);
    }}
    .task-child-row {{
      position: relative;
      margin: 6px 0 6px 0;
      padding-left: 8px;
      border: 1px dashed rgba(14, 116, 144, 0.18);
      border-left: 0;
      background: var(--brand-surface-muted);
    }}
    .task-child-row::before {{
      content: "↳";
      position: absolute;
      left: -18px;
      top: 13px;
      color: var(--brand-accent-strong);
      font-weight: 700;
    }}
    .task-meta {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
      margin-top: 6px;
      color: var(--brand-text-mute);
      font-size: 0.78rem;
    }}
    .task-detail {{
      grid-column: 2 / 4;
      display: grid;
      gap: 8px;
      margin: 2px 0 4px;
      padding: 10px 12px;
      border: 1px solid var(--brand-border);
      border-radius: var(--brand-radius);
      background: var(--brand-surface-muted);
      color: var(--brand-text-soft);
      font: var(--font-small);
    }}
    .task-detail[hidden] {{ display: none; }}
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px 12px;
    }}
    .detail-label {{
      display: block;
      color: var(--brand-text-mute);
      font: var(--font-eyebrow);
      text-transform: uppercase;
    }}
    .detail-value {{
      overflow-wrap: anywhere;
    }}
    .task-action {{
      align-self: start;
      transition: transform 120ms ease;
    }}
    .task-row.is-open .task-action {{
      transform: translateY(1px);
      border-color: rgba(14, 116, 144, 0.28);
      background: var(--brand-accent-soft);
      color: var(--brand-accent-strong);
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 1px 7px;
      border: 1px solid var(--brand-border);
      border-radius: 10px;
      background: var(--brand-surface-muted);
      color: var(--brand-text-mute);
      font: 400 0.72rem/1.45 var(--brand-font-mono);
      white-space: nowrap;
    }}
    .pill.ok {{
      border-color: rgba(6, 118, 71, 0.24);
      background: var(--brand-ok-soft);
      color: var(--brand-ok);
    }}
    .pill.warn {{
      border-color: rgba(180, 83, 9, 0.28);
      background: var(--brand-amber-soft);
      color: var(--brand-amber);
    }}
    .pill.danger {{
      border-color: rgba(180, 35, 24, 0.25);
      background: var(--brand-danger-soft);
      color: var(--brand-danger);
    }}
    .empty {{
      padding: 28px 0;
      color: var(--brand-text-mute);
      font: var(--font-small);
    }}
    .request-row {{
      display: grid;
      gap: 4px;
      padding: 12px 0;
      border-bottom: 1px solid #eef1f5;
    }}
    .request-row .title {{
      font-weight: 600;
      overflow-wrap: anywhere;
    }}
    .request-row .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      color: var(--brand-text-mute);
      font: var(--font-meta);
    }}
    .assignee-block {{
      margin-top: 16px;
    }}
    .assignee-block h3 {{
      margin: 0;
      padding: 8px 0;
      color: var(--brand-text-mute);
      font: var(--font-h4);
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    .rail-panel {{
      display: grid;
      gap: var(--brand-space-3);
    }}
    .panel {{
      border: 1px solid var(--brand-border);
      border-radius: var(--brand-radius-md);
      background: var(--brand-surface);
      padding: var(--brand-space-4);
      scroll-margin-top: 72px;
    }}
    .panel h2 {{
      margin: 0 0 10px;
      color: var(--brand-text);
      font: var(--font-h3);
    }}
    .panel p {{
      margin: 0;
      color: var(--brand-text-mute);
      font: var(--font-small);
    }}
    .surface-card {{
      display: grid;
      gap: 4px;
      padding: 10px 0;
      border-top: 1px solid #eef1f5;
      scroll-margin-top: 72px;
    }}
    .surface-card:target {{
      margin-inline: -8px;
      padding-inline: 8px;
      border-radius: var(--brand-radius);
      background: var(--brand-accent-soft);
    }}
    .surface-card:first-of-type {{ border-top: 0; }}
    .surface-card strong {{
      font: var(--font-body-tight);
      color: var(--brand-text);
    }}
    .surface-card span {{
      color: var(--brand-text-mute);
      font: var(--font-small);
    }}
    .event-list {{
      display: grid;
      gap: 8px;
    }}
    .event-item {{
      display: grid;
      gap: 2px;
      padding-top: 8px;
      border-top: 1px solid #eef1f5;
    }}
    .event-item:first-child {{
      padding-top: 0;
      border-top: 0;
    }}
    .event-type {{
      color: var(--brand-accent-strong);
      font: var(--font-meta);
      overflow-wrap: anywhere;
    }}
    .event-detail {{
      color: var(--brand-text-mute);
      font: var(--font-small);
      overflow-wrap: anywhere;
    }}
    .timeline-list {{
      display: grid;
      gap: 4px;
      margin-top: 8px;
    }}
    @media (max-width: 1120px) {{
      .board-grid {{ grid-template-columns: 1fr; }}
      .rail-panel {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 920px) {{
      .app {{ grid-template-columns: 1fr; }}
      .sidebar {{ position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--brand-border); }}
      main.board {{ padding: 28px 20px 72px; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .ops-strip {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .topbar {{ padding: 0 var(--brand-space-4); }}
      .topbar-stat {{ display: none; }}
    }}
    @media (max-width: 640px) {{
      .topbar {{ gap: var(--brand-space-3); }}
      .brand .inst {{ display: none; }}
      .rail-panel {{ grid-template-columns: 1fr; }}
      .ops-strip {{ grid-template-columns: 1fr; }}
      .task-row {{ grid-template-columns: 24px minmax(0, 1fr); }}
      .task-row > .pill:last-child {{ grid-column: 2; width: max-content; }}
      .task-detail {{ grid-column: 2; }}
      .detail-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <span class="inst">TASK_MANAGEMENT</span>
      <span class="lead">Task Page</span>
    </div>
    <nav class="topnav" aria-label="surface roles">
      <a class="active" href="#board" aria-current="page">웹 task page</a>
      <a href="#surface-personal_chat">개인 DM</a>
      <a href="#surface-team_room">팀 공유방</a>
    </nav>
    <div class="topbar-stat"><span class="dot"></span><span>{event_count} events</span></div>
  </header>
  <div class="app">
    <aside class="sidebar">
      <div class="side-group">
        <div class="side-h">Board</div>
        {_nav_link("today", nav_counts, active=True)}
        {_nav_link("this_week", nav_counts)}
        {_nav_link("pending_approvals", nav_counts)}
        {_nav_link("questions", nav_counts)}
        {_nav_link("floating", nav_counts)}
        {_nav_link("routines", nav_counts)}
        {_nav_link("prep_subtasks", nav_counts)}
        {_nav_link("reminders", nav_counts)}
        {_nav_link("references", nav_counts)}
        {_nav_link("by_assignee", nav_counts)}
        {_nav_link("done", nav_counts)}
      </div>
      <div class="side-group">
        <div class="side-h">Surfaces</div>
        {_surface_roles(model["surface_roles"])}
      </div>
      <div class="surface-note">DM은 빠른 입력·질문, 웹은 triage·검증, 팀방은 future</div>
    </aside>
    <main class="board" id="board">
      <div class="board-grid">
        <div>
          <div class="board-head">
            <div class="board-eyebrow">TASK_MANAGEMENT OPS · {escape(model.get("today_label", model["today"]))}</div>
            <h1 class="board-title">TeamTask Task Page</h1>
            <div class="board-sub">Slack DM에서 들어온 줄글을 proposal로 정리하고, 미해결 질문·충돌·루틴·리마인더·task-core export 후보 상태를 한 화면에서 triage합니다. task-core는 여전히 preview-only로 검증하며 파일을 쓰지 않습니다.</div>
          </div>
          <div class="metrics">
            {_metric("전체", counts["total"])}
            {_metric("승인됨", counts["approved"])}
            {_metric("승인 대기", counts["awaiting_approval"])}
            {_metric("질문", counts["questions"])}
            {_metric("floating", counts["floating"])}
            {_metric("루틴", counts["routines"])}
            {_metric("준비", counts["prep_subtasks"])}
            {_metric("참고", counts["references"])}
            {_metric("완료", counts["done"])}
          </div>
          <div class="ops-strip" aria-label="task-core preview readiness">
            {_ops_card("Export 후보", preview_counts.get("ready", 0), "승인 완료·필수 정보 충족")}
            {_ops_card("확인 필요", preview_counts.get("blocked", 0), "질문·승인·누락 슬롯 대기")}
            {_ops_card("적용 표시", preview_counts.get("applied", 0), "로컬 export id 기록됨")}
            {_ops_card("제외", preview_counts.get("excluded", 0), "거절·완료 등 preview 제외")}
          </div>
          {_section("today", "오늘", sections["today"])}
          {_section("this_week", "이번 주", sections["this_week"])}
          {_request_section("pending_approvals", "승인 대기", sections["pending_approvals"])}
          {_section("questions", "미해결 질문", sections["questions"])}
          {_section("floating", "떠 있는 항목", sections["floating"])}
          {_section("routines", "반복 루틴", sections["routines"])}
          {_section("prep_subtasks", "준비 작업", sections["prep_subtasks"])}
          {_reminder_section("reminders", "리마인더", sections["reminders"])}
          {_section("references", "참고 링크", sections["references"])}
          {_assignee_section("by_assignee", "담당자별", sections["by_assignee"])}
          {_section("done", "완료", sections["done"])}
        </div>
        <aside class="rail-panel" aria-label="dashboard details">
          {_task_core_panel(preview_counts)}
          {_surface_panel(model["surface_roles"])}
          {_status_panel(model.get("status_counts", {}))}
          {_events_panel(model.get("recent_events", ()))}
        </aside>
      </div>
    </main>
  </div>
  <script>
    (() => {{
      const navLinks = Array.from(document.querySelectorAll("[data-nav-target]"));
      const topLinks = Array.from(document.querySelectorAll(".topnav a"));
      const setActive = (id) => {{
        navLinks.forEach((link) => {{
          const active = link.dataset.navTarget === id;
          link.classList.toggle("active", active);
          if (active) {{
            link.setAttribute("aria-current", "location");
          }} else {{
            link.removeAttribute("aria-current");
          }}
        }});
      }};
      const setTopActive = (hash) => {{
        const normalized = hash || "#board";
        topLinks.forEach((link) => {{
          const href = link.getAttribute("href") || "#board";
          const active =
            href === normalized ||
            (href === "#board" && !normalized.startsWith("#surface-")) ||
            (href === "#board" && normalized === "#surface-web_task_page");
          link.classList.toggle("active", active);
          if (active) {{
            link.setAttribute("aria-current", "page");
          }} else {{
            link.removeAttribute("aria-current");
          }}
        }});
      }};
      const syncActiveFromHash = () => {{
        const hash = window.location.hash || "#today";
        const id = hash.slice(1);
        if (id) setActive(id);
        setTopActive(hash);
      }};
      navLinks.forEach((link) => {{
        link.addEventListener("click", () => setActive(link.dataset.navTarget || ""));
      }});
      topLinks.forEach((link) => {{
        link.addEventListener("click", () => setTopActive(link.getAttribute("href") || "#board"));
      }});
      window.addEventListener("hashchange", syncActiveFromHash);
      syncActiveFromHash();

      document.querySelectorAll(".task-row[role='button']").forEach((row) => {{
        const detail = row.querySelector(".task-detail");
        const action = row.querySelector(".task-action");
        if (!detail) return;
        const setOpen = (open) => {{
          row.classList.toggle("is-open", open);
          row.setAttribute("aria-expanded", String(open));
          detail.hidden = !open;
          if (action) action.textContent = open ? "닫기" : "열기";
        }};
        row.addEventListener("click", (event) => {{
          if (event.target.closest("a, button")) return;
          setOpen(row.getAttribute("aria-expanded") !== "true");
        }});
        row.addEventListener("keydown", (event) => {{
          if (event.key === "Enter" || event.key === " ") {{
            event.preventDefault();
            setOpen(row.getAttribute("aria-expanded") !== "true");
          }}
        }});
      }});
    }})();
  </script>
</body>
</html>
"""


def _proposal_date(proposal: Proposal) -> date | None:
    return proposal.due_date or proposal.scheduled_date


def _belongs_in_today_section(proposal: Proposal, *, today: date) -> bool:
    return (is_open_work_item(proposal) and _proposal_date(proposal) == today) or _is_open_overdue(proposal, today=today)


def _is_open_status(proposal: Proposal) -> bool:
    return is_open_work_item(proposal)


def _is_open_overdue(proposal: Proposal, *, today: date) -> bool:
    return (
        proposal.status in {"approved", "applied", "awaiting_approval"}
        and needs_time_resolution(proposal, today=today)
        and not has_deferred_missing_info(proposal)
    )


def _is_overdue(proposal: Proposal, *, today: date) -> bool:
    return (
        proposal.status in {"draft", "posted", "awaiting_approval", "approved", "applied"}
        and needs_time_resolution(proposal, today=today)
        and not has_deferred_missing_info(proposal)
    )


def _proposal_view(
    proposal: Proposal,
    applied_export: dict[str, str] | None = None,
    *,
    today: date,
    events: tuple[dict[str, Any], ...] = (),
    proposals: tuple[Proposal, ...] = (),
) -> dict[str, Any]:
    proposal_date = _proposal_date(proposal)
    preview_status = _preview_status(proposal, applied_export)
    conflict_ids = proposal.metadata.get(CONFLICT_WITH_PROPOSAL_IDS_KEY, "")
    if not conflict_ids and proposal.metadata.get(CONFLICT_DETECTED_KEY) == "true":
        conflict_ids = "충돌 확인 필요"
    is_container = is_workflow_context_parent(proposal)
    is_overdue = _is_overdue(proposal, today=today) and not _suppress_parent_overdue(proposal, proposals)
    urgency_label = work_item_urgency_label(proposal, today=today) if is_overdue else ""
    projection = workflow_projection(proposal, proposals) if proposals and is_workflow_parent(proposal, proposals) else None
    timeline = proposal_timeline(events, proposals, proposal_id=proposal.proposal_id) if events and proposals else ()
    view: dict[str, Any] = {
        "proposal_id": proposal.proposal_id,
        "source_message_id": proposal.source_message_id,
        "title": proposal.title,
        "status": proposal.status,
        "status_label": proposal_status_label(proposal.status),
        "kind": proposal.kind,
        "assigned_to": proposal.assigned_to,
        "task_management_area": proposal.task_management_area,
        "date": "" if is_container else proposal_date.isoformat() if proposal_date else "",
        "date_label": "" if is_container else date_label(proposal_date) if proposal_date else "",
        "is_overdue": "true" if is_overdue else "",
        "urgency_label": urgency_label,
        "time_window": "" if is_container else proposal.time_window,
        "date_window": _date_window_label(proposal),
        DATE_WINDOW_KIND_KEY: proposal.metadata.get(DATE_WINDOW_KIND_KEY, ""),
        "missing_slots": ", ".join(proposal.missing_slots),
        PARTICIPANTS_KEY: _participants_label(proposal),
        LOCATION_KEY: proposal.metadata.get(LOCATION_KEY, ""),
        "parent_proposal_id": proposal.metadata.get("parent_proposal_id", ""),
        "step_label": step_label(proposal, proposals),
        "workflow_id": proposal.metadata.get("workflow_id", ""),
        "workflow_title": proposal.metadata.get("workflow_title", ""),
        "rollup_status": projection.rollup_status if projection else "",
        "current_next_step": projection.current_next_step.title if projection and projection.current_next_step else "",
        "blocking_dependencies": ", ".join(item.title for item in projection.blocking_dependencies) if projection else "",
        "preview_status": preview_status,
        "preview_label": PREVIEW_LABELS[preview_status],
        "export_item_id": applied_export.get("export_item_id", "") if applied_export else "",
        "applied_at": applied_export.get("applied_at", "") if applied_export else "",
        DEFERRED_UNTIL_KEY: proposal.metadata.get(DEFERRED_UNTIL_KEY, ""),
        CONFLICT_DETECTED_KEY: proposal.metadata.get(CONFLICT_DETECTED_KEY, ""),
        CONFLICT_WITH_PROPOSAL_IDS_KEY: conflict_ids,
        PROGRESS_STATUS_KEY: proposal.metadata.get(PROGRESS_STATUS_KEY, ""),
        PROGRESS_NOTE_KEY: proposal.metadata.get(PROGRESS_NOTE_KEY, ""),
        REMAINING_WORK_KEY: proposal.metadata.get(REMAINING_WORK_KEY, ""),
        PROGRESS_UPDATED_AT_KEY: proposal.metadata.get(PROGRESS_UPDATED_AT_KEY, ""),
        "children": [],
        "child_total_count": 0,
        "child_done_count": 0,
        "hidden_completed_child_count": 0,
        "timeline": [entry.to_view() for entry in timeline],
    }
    return view


def _request_view(request: ApprovalRequest, proposals: tuple[Proposal, ...]) -> dict[str, str]:
    proposal = next((item for item in proposals if item.proposal_id == request.proposal_id), None)
    title = proposal.title if proposal is not None else request.proposal_id
    return {
        "request_id": request.request_id,
        "proposal_id": request.proposal_id,
        "approver_id": request.approver_id,
        "title": title,
        "status": request.status,
    }


def _request_surface_visible(request: ApprovalRequest, proposals: tuple[Proposal, ...]) -> bool:
    proposal = next((item for item in proposals if item.proposal_id == request.proposal_id), None)
    return proposal is None or is_surface_visible_item(proposal)


def _by_assignee(
    proposals: tuple[Proposal, ...],
    applied_exports: dict[str, dict[str, str]],
    *,
    today: date,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for proposal in sorted(proposals, key=lambda item: proposal_deadline_sort_key(item, today=today, overdue_last=True)):
        grouped.setdefault(proposal.assigned_to, []).append(
            _proposal_view(proposal, applied_exports.get(proposal.proposal_id), today=today, proposals=proposals)
        )
    return grouped


def _hierarchy_section(
    section_proposals: list[Proposal],
    *,
    proposals: tuple[Proposal, ...],
    proposal_views: dict[str, dict[str, Any]],
    max_completed_children: int | None = None,
) -> list[dict[str, Any]]:
    visible_ids = {proposal.proposal_id for proposal in section_proposals}
    proposals_by_id = {proposal.proposal_id: proposal for proposal in proposals}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for proposal in section_proposals:
        anchor = section_anchor(proposal, proposals_by_id, visible_ids)
        if anchor.proposal_id in seen:
            continue
        seen.add(anchor.proposal_id)
        item = dict(proposal_views[anchor.proposal_id])
        all_children = tuple(child for child in child_proposals(anchor, proposals) if is_surface_visible_item(child))
        children_to_render = display_children(all_children, max_completed_children=max_completed_children)
        children = [
            dict(proposal_views[child.proposal_id])
            for child in children_to_render
        ]
        item["children"] = children
        item["child_total_count"] = len(all_children)
        item["child_done_count"] = _completed_proposal_count(all_children)
        item["hidden_completed_child_count"] = max(0, _completed_proposal_count(all_children) - _completed_proposal_count(children_to_render))
        result.append(item)
    return result


def _completed_proposal_count(proposals: tuple[Proposal, ...]) -> int:
    return sum(1 for proposal in proposals if proposal.status in {"done", "applied"})


def _suppress_parent_overdue(proposal: Proposal, proposals: tuple[Proposal, ...]) -> bool:
    if not is_workflow_context_parent(proposal):
        return False
    return bool(child_proposals(proposal, proposals))


def _status_counts(proposals: tuple[Proposal, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for proposal in proposals:
        counts[proposal.status] = counts.get(proposal.status, 0) + 1
    return counts


def _preview_counts(
    proposals: tuple[Proposal, ...],
    applied_exports: dict[str, dict[str, str]],
) -> dict[str, int]:
    counts = {key: 0 for key in PREVIEW_LABELS}
    for proposal in proposals:
        status = _preview_status(proposal, applied_exports.get(proposal.proposal_id))
        counts[status] += 1
    return counts


def _preview_status(proposal: Proposal, applied_export: dict[str, str] | None = None) -> str:
    if applied_export is not None or proposal.status == "applied":
        return "applied"
    if proposal.status in {"rejected", "done"}:
        return "excluded"
    if proposal.status == "approved" and not proposal.missing_slots:
        return "ready"
    if proposal.status in {"draft", "posted", "awaiting_approval"} or proposal.kind == "question" or proposal.missing_slots:
        return "blocked"
    return "excluded"


def _nav_link(key: str, counts: dict[str, int], *, active: bool = False) -> str:
    klass = "side-link active" if active else "side-link"
    current = ' aria-current="location"' if active else ""
    return (
        f'<a class="{klass}" href="#{escape(key)}" data-nav-target="{escape(key)}"{current}>'
        f'<span>{escape(BOARD_LABELS[key])}</span><span class="count">{counts.get(key, 0)}</span></a>'
    )


def _surface_roles(roles: dict[str, dict[str, str]]) -> str:
    return "".join(
        f"""<a class="side-link" href="#surface-{escape(key)}">
  <span>{escape(role["label"])}</span>
</a>"""
        for key, role in roles.items()
    )


def _metric(label: str, value: int) -> str:
    return f'<div class="metric"><strong>{value}</strong><span>{escape(label)}</span></div>'


def _ops_card(label: str, value: int, hint: str) -> str:
    return (
        f'<div class="ops-card"><strong>{escape(str(value))} · {escape(label)}</strong>'
        f"<span>{escape(hint)}</span></div>"
    )


def _section(key: str, title: str, items: list[dict[str, Any]]) -> str:
    body = "".join(_proposal_row(item, scope=key) for item in items) if items else '<div class="empty">표시할 항목이 없습니다.</div>'
    return f"""<section class="section" id="{escape(key)}">
  <div class="section-head"><span class="title">{escape(title)}</span><span class="count">{len(items)}</span></div>
  <div class="section-body">{body}</div>
</section>"""


def _request_section(key: str, title: str, items: list[dict[str, str]]) -> str:
    if not items:
        body = '<div class="empty">표시할 항목이 없습니다.</div>'
    else:
        body = "".join(
            f"""<div class="request-row">
  <div class="title">{escape(item["title"])}</div>
  <div class="meta"><span>요청 {escape(item["request_id"])}</span><span>승인자 {escape(item["approver_id"])}</span></div>
</div>"""
            for item in items
        )
    return f"""<section class="section" id="{escape(key)}">
  <div class="section-head"><span class="title">{escape(title)}</span><span class="count">{len(items)}</span></div>
  <div class="section-body">{body}</div>
</section>"""


def _reminder_section(key: str, title: str, items: list[dict[str, str]]) -> str:
    if not items:
        body = '<div class="empty">표시할 항목이 없습니다.</div>'
    else:
        body = "".join(
            f"""<div class="request-row">
  <div class="title">{escape(item.get("dedupe_key", ""))}</div>
  <div class="meta"><span>{escape(item.get("proposal_id", ""))}</span><span>{escape(item.get("occurred_at", ""))}</span></div>
</div>"""
            for item in items
        )
    return f"""<section class="section" id="{escape(key)}">
  <div class="section-head"><span class="title">{escape(title)}</span><span class="count">{len(items)}</span></div>
  <div class="section-body">{body}</div>
</section>"""


def _assignee_section(key: str, title: str, groups: dict[str, list[dict[str, Any]]]) -> str:
    if not groups:
        body = '<div class="empty">표시할 항목이 없습니다.</div>'
    else:
        chunks = []
        for assignee, items in sorted(groups.items()):
            chunks.append(f'<div class="assignee-block"><h3>{escape(assignee)}</h3>')
            chunks.extend(_proposal_row(item, scope=f"{key}-{assignee}") for item in items)
            chunks.append("</div>")
        body = "".join(chunks)
    return f"""<section class="section" id="{escape(key)}">
  <div class="section-head"><span class="title">{escape(title)}</span><span class="count">{len(groups)}</span></div>
  <div class="section-body">{body}</div>
</section>"""


def _proposal_row(item: dict[str, Any], *, scope: str, child: bool = False) -> str:
    detail_id = _html_id("task-detail", f"{scope}-{item['proposal_id']}")
    pills = [
        _pill(item["preview_label"], PREVIEW_PILL_CLASS.get(item["preview_status"], "")),
        _pill(item["kind"]),
        _pill(item["assigned_to"]),
        _pill(item["task_management_area"]),
    ]
    if item["urgency_label"]:
        pills.append(_pill(item["urgency_label"], "danger"))
    display_date = item.get("date_label") or item["date"]
    if display_date:
        pills.append(_pill(display_date, "ok"))
    if item["time_window"]:
        pills.append(_pill(f"시간 {item['time_window']}", "ok"))
    if item[PARTICIPANTS_KEY]:
        pills.append(_pill(f"참석 {item[PARTICIPANTS_KEY]}"))
    if item[LOCATION_KEY]:
        pills.append(_pill(f"장소 {item[LOCATION_KEY]}"))
    if item["date_window"]:
        pills.append(_pill(item["date_window"], "warn"))
    if item[PROGRESS_STATUS_KEY]:
        pills.append(_pill(f"진행 {item[PROGRESS_STATUS_KEY]}", "warn"))
    if item[REMAINING_WORK_KEY]:
        pills.append(_pill(f"남은 일 {item[REMAINING_WORK_KEY]}", "warn"))
    if item["missing_slots"]:
        pills.append(_pill(f"확인: {item['missing_slots']}", "danger"))
    if item[CONFLICT_WITH_PROPOSAL_IDS_KEY]:
        pills.append(_pill(f"충돌: {item[CONFLICT_WITH_PROPOSAL_IDS_KEY]}", "danger"))
    if item[DEFERRED_UNTIL_KEY]:
        pills.append(_pill(f"리마인드 {item[DEFERRED_UNTIL_KEY]}", "warn"))
    if item.get("step_label"):
        pills.append(_pill(f"단계 {item['step_label']}", "ok"))
    if item.get("rollup_status"):
        pills.append(_pill(f"흐름 {item['rollup_status']}", "warn"))
    if item.get("current_next_step"):
        pills.append(_pill(f"다음 {item['current_next_step']}", "warn"))
    if item.get("blocking_dependencies"):
        pills.append(_pill(f"막힘 {item['blocking_dependencies']}", "danger"))
    children = list(item.get("children", []))
    if children:
        done_count = int(item.get("child_done_count") or _completed_child_count(children))
        total_count = int(item.get("child_total_count") or len(children))
        pill_class = "ok" if done_count == total_count else "warn"
        pills.append(_pill(f"하위작업 {done_count}/{total_count} 완료", pill_class))
    child_class = " task-child-row" if child else ""
    child_rows = "".join(
        _proposal_row(child_item, scope=f"{scope}-child", child=True)
        for child_item in children
    )
    children_html = (
        f'<div class="task-children"><div class="task-children-label">'
        f'{escape(_children_summary_label(item, children))}</div>{child_rows}</div>'
        if child_rows
        else ""
    )
    return f"""<article class="task-row{child_class}" data-status="{escape(item["status"])}" data-preview="{escape(item["preview_status"])}" data-overdue="{escape(item["is_overdue"])}" role="button" tabindex="0" aria-expanded="false" aria-controls="{escape(detail_id)}">
  <span class="prio-check" aria-hidden="true"></span>
  <div class="task-body">
    <div class="task-title">{escape(item["title"])}</div>
    <div class="task-meta"><span class="pill">{escape(item["status_label"])}</span>{''.join(pills)}</div>
  </div>
  <span class="pill task-action">열기</span>
  {_proposal_detail(detail_id, item)}
</article>{children_html}"""


def _completed_child_count(children: list[dict[str, Any]]) -> int:
    return sum(1 for child in children if child.get("status") in {"done", "applied"})


def _children_summary_label(parent: dict[str, Any], children: list[dict[str, Any]]) -> str:
    done_count = int(parent.get("child_done_count") or _completed_child_count(children))
    next_child = next((child for child in children if child.get("status") not in {"done", "applied", "rejected"}), None)
    if next_child:
        step = str(next_child.get("step_label") or "")
        next_label = f" · 다음 {step}" if step else ""
    else:
        next_label = ""
    total_count = int(parent.get("child_total_count") or len(children))
    hidden_count = int(parent.get("hidden_completed_child_count") or 0)
    hidden_label = f" · 완료 {hidden_count}개 숨김" if hidden_count else ""
    return f"하위작업 {done_count}/{total_count} 완료{next_label}{hidden_label}"


def _pill(text: str, klass: str = "") -> str:
    if not text:
        return ""
    class_name = "pill" if not klass else f"pill {klass}"
    return f'<span class="{class_name}">{escape(text)}</span>'


def _task_core_panel(preview_counts: dict[str, int]) -> str:
    body = "".join(
        _pill(f"{PREVIEW_LABELS[key]} {preview_counts.get(key, 0)}", PREVIEW_PILL_CLASS.get(key, ""))
        for key in ("ready", "blocked", "applied", "excluded")
    )
    return f"""<section class="panel">
  <h2>Task-core preview</h2>
  <p>승인된 proposal은 export 후보로 표시되며, 실제 검증은 export-preview/demo가 task-core.export.v1 preview API로 수행합니다. task-core inbox/raw/wiki 파일은 쓰지 않습니다. <code>mutates_files=false</code></p>
  <div class="task-meta">{body}</div>
</section>"""


def _surface_panel(roles: dict[str, dict[str, str]]) -> str:
    cards = "".join(
        f"""<div class="surface-card" id="surface-{escape(key)}">
  <strong>{escape(role["label"])}</strong>
  <span>{escape(role["primary_role"])}</span>
  <span>뜨는 조건: {escape(role["main_when"])}</span>
</div>"""
        for key, role in roles.items()
    )
    return f"""<section class="panel">
  <h2>Frontend surfaces</h2>
  {cards}
</section>"""


def _status_panel(status_counts: dict[str, int]) -> str:
    if not status_counts:
        body = '<p>아직 상태가 없습니다.</p>'
    else:
        body = '<div class="task-meta">' + "".join(
            _pill(f"{proposal_status_label(status)} {count}") for status, count in sorted(status_counts.items())
        ) + "</div>"
    return f"""<section class="panel">
  <h2>Status</h2>
  {body}
</section>"""


def _events_panel(events: tuple[dict[str, Any], ...]) -> str:
    if not events:
        body = '<p>아직 이벤트가 없습니다.</p>'
    else:
        body = '<div class="event-list">' + "".join(_event_item(event) for event in reversed(events)) + "</div>"
    return f"""<section class="panel">
  <h2>Recent events</h2>
  {body}
</section>"""


def _event_item(event: dict[str, Any]) -> str:
    event_type = str(event.get("type", "event"))
    event_at = str(event.get("occurred_at") or event.get("at") or "")
    subject = str(
        event.get("proposal_id")
        or event.get("request_id")
        or event.get("message_id")
        or event.get("source_message_id")
        or event.get("payload", {}).get("proposal_id")
        or event.get("payload", {}).get("request_id")
        or event.get("payload", {}).get("message_id")
        or event.get("payload", {}).get("source_message_id")
        or ""
    )
    detail = " · ".join(part for part in (event_at, subject) if part)
    return f"""<div class="event-item">
  <div class="event-type">{escape(event_type)}</div>
  <div class="event-detail">{escape(detail)}</div>
</div>"""


def _date_window_label(proposal: Proposal) -> str:
    return proposal_date_window_label(proposal)


def _proposal_detail(detail_id: str, item: dict[str, Any]) -> str:
    fields = [
        ("상태", item["status_label"]),
        ("Preview", item["preview_label"]),
        ("종류", item["kind"]),
        ("담당", item["assigned_to"]),
        ("영역", item["task_management_area"]),
        ("강조", item["urgency_label"]),
        ("날짜", item.get("date_label") or item["date"]),
        ("시간", item["time_window"]),
        ("가능 기간", item["date_window"]),
        ("기간 유형", item[DATE_WINDOW_KIND_KEY]),
        ("참석", item[PARTICIPANTS_KEY]),
        ("장소", item[LOCATION_KEY]),
        ("확인 필요", item["missing_slots"]),
        ("충돌", item[CONFLICT_WITH_PROPOSAL_IDS_KEY]),
        ("리마인드", item[DEFERRED_UNTIL_KEY]),
        ("진행 상태", item[PROGRESS_STATUS_KEY]),
        ("남은 일", item[REMAINING_WORK_KEY]),
        ("진행 메모", item[PROGRESS_NOTE_KEY]),
        ("진행 갱신", item[PROGRESS_UPDATED_AT_KEY]),
        ("Export item", item["export_item_id"]),
        ("Applied at", item["applied_at"]),
        ("Proposal", item["proposal_id"]),
        ("Source", item["source_message_id"]),
        ("Parent", item.get("parent_proposal_id", "")),
        ("Workflow", item.get("workflow_title", "") or item.get("workflow_id", "")),
        ("Step", item.get("step_label", "")),
        ("Rollup", item.get("rollup_status", "")),
        ("Current next", item.get("current_next_step", "")),
        ("Blockers", item.get("blocking_dependencies", "")),
    ]
    chunks = "".join(
        f"""<div>
  <span class="detail-label">{escape(label)}</span>
  <span class="detail-value">{escape(value)}</span>
</div>"""
        for label, value in fields
        if value
    )
    timeline = item.get("timeline", [])
    timeline_html = ""
    if timeline:
        timeline_html = '<div class="timeline-list">' + "".join(
            f"""<div class="event-item">
  <div class="event-type">{escape(str(entry.get("event_type", "")))}</div>
  <div class="event-detail">{escape(str(entry.get("occurred_at", "")))} · {escape(str(entry.get("proposal_title", "")))}</div>
</div>"""
            for entry in timeline[:8]
        ) + "</div>"
    return f"""<div class="task-detail" id="{escape(detail_id)}" hidden>
  <div class="detail-grid">{chunks}</div>
  {timeline_html}
</div>"""


def _html_id(prefix: str, value: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return f"{prefix}-{slug or 'item'}"


def _participants_label(proposal: Proposal) -> str:
    return proposal_participants_label(proposal, translate=False)
