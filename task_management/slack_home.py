from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .frontend import build_web_task_page_model
from .human_view import datetime_label, render_missing_slot_labels
from .slack_adapter import SlackDmAdapter
from .sort_keys import time_sort_minutes
from .store import TeamTaskStore


def build_slack_home_view(
    store: TeamTaskStore,
    *,
    now: datetime,
    dashboard_url: str = "",
    max_items: int = 8,
) -> dict[str, Any]:
    """Build a compact Slack App Home view from the same state as the web task page."""

    model = build_web_task_page_model(store, today=now.date())
    sections = model["sections"]
    today_items = list(sections["today"])
    today_ids = {item["proposal_id"] for item in today_items}
    week_items = [item for item in sections["this_week"] if item["proposal_id"] not in today_ids]
    attention_items = _attention_items(sections)
    counts = model["counts"]

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Task Management", "emoji": True}},
        {
            "type": "context",
            "elements": [
                _mrkdwn(
                    f"마지막 갱신: {datetime_label(now)} · "
                    f"오늘 {len(today_items)} · 이번 주 {len(week_items)} · 확인 필요 {len(attention_items)}"
                )
            ],
        },
    ]
    if dashboard_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "웹 대시보드 열기", "emoji": True},
                        "url": dashboard_url,
                    }
                ],
            }
        )
    blocks.extend(
        [
            {"type": "divider"},
            _list_section("오늘", today_items, empty="오늘 날짜로 잡힌 항목이 없습니다.", max_items=max_items),
            _list_section(
                "이번 주",
                week_items,
                empty="오늘 이후 7일 안에 잡힌 항목이 없습니다.",
                max_items=max_items,
            ),
            _text_section(
                "확인 필요",
                _attention_lines(attention_items, max_items=max_items)
                or ["확인/승인/누락 슬롯이 필요한 항목이 없습니다."],
            ),
            {
                "type": "context",
                "elements": [
                    _mrkdwn(
                        "전체 "
                        f"{counts['total']} · 승인됨 {counts['approved']} · 승인 대기 {counts['awaiting_approval']} · "
                        f"질문 {counts['questions']} · 떠 있음 {counts['floating']}"
                    )
                ],
            },
        ]
    )
    return {"type": "home", "blocks": blocks[:100]}


def publish_slack_home_tab(
    store: TeamTaskStore,
    adapter: SlackDmAdapter,
    *,
    user_id: str = "",
    now: datetime,
    dashboard_url: str = "",
) -> bool:
    """Publish the current task state to Slack App Home without crashing live intake."""

    target_user_id = user_id or adapter.config.user_id
    if not target_user_id:
        store.append_event(
            "slack.home.publish.skipped",
            {"reason": "missing_user_id"},
            occurred_at=now,
        )
        return False

    view = build_slack_home_view(store, now=now, dashboard_url=dashboard_url)
    try:
        provider_view_id = adapter.publish_home(target_user_id, view)
    except Exception as exc:
        store.append_event(
            "slack.home.publish.failed",
            {
                "user_id": target_user_id,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
            occurred_at=now,
        )
        return False

    store.append_event(
        "slack.home.published",
        {
            "user_id": target_user_id,
            "provider_view_id": provider_view_id,
            "block_count": len(view["blocks"]),
            "dashboard_url": dashboard_url,
        },
        occurred_at=now,
    )
    return True


def _list_section(title: str, items: list[Mapping[str, str]], *, empty: str, max_items: int) -> dict[str, Any]:
    lines = [_proposal_line(item) for item in items[:max_items]]
    if len(items) > max_items:
        lines.append(f"외 {len(items) - max_items}개 더 있음")
    return _text_section(title, lines or [empty])


def _text_section(title: str, lines: list[str]) -> dict[str, Any]:
    text = f"*{_escape(title)}*\n" + "\n".join(f"• {line}" for line in lines)
    return {"type": "section", "text": _mrkdwn(text)}


def _proposal_line(item: Mapping[str, str]) -> str:
    detail = " · ".join(
        part
        for part in (
            "🔴 마감 지남" if item.get("is_overdue") == "true" else "",
            item.get("date_label", "") or item.get("date", ""),
            item.get("time_window", ""),
            item.get("status_label", ""),
            f"진행: {item.get('progress_status', '')}" if item.get("progress_status") else "",
            f"남은 일: {item.get('remaining_work', '')}" if item.get("remaining_work") else "",
            f"확인: {_missing_slots_label(item)}" if item.get("missing_slots") else "",
        )
        if part
    )
    title = _escape(item.get("title", ""))
    line = f"*{title}*" + (f" — {_escape(detail)}" if detail else "")
    children = [child for child in item.get("children", []) if isinstance(child, Mapping)]  # type: ignore[attr-defined]
    child_lines = []
    if children:
        child_lines.append(_subtask_summary_line(children))
    for index, child in enumerate(children):
        if not isinstance(child, Mapping):
            continue
        connector = "└─" if index == len(children) - 1 else "├─"
        child_lines.append(f"    {connector} {_subtask_line(child)}")
    if child_lines:
        line += "\n" + "\n".join(child_lines)
    return line


def _subtask_summary_line(children: list[Mapping[str, str]]) -> str:
    done_count = sum(1 for child in children if child.get("status") in {"done", "applied"})
    next_child = next((child for child in children if child.get("status") not in {"done", "applied", "rejected"}), None)
    next_label = ""
    if next_child is not None:
        step = str(next_child.get("step_label") or "")
        title = _escape(str(next_child.get("title") or ""))
        next_label = f" · 다음: {step} {title}" if step else f" · 다음: {title}"
    return f"  ↳ _하위작업 {done_count}/{len(children)} 완료{next_label}_"


def _subtask_line(child: Mapping[str, str]) -> str:
    step = str(child.get("step_label") or "")
    step_prefix = f"[{_escape(step)}] " if step else ""
    title = _escape(str(child.get("title") or ""))
    detail = " · ".join(
        part
        for part in (
            _status_display(child),
            child.get("date_label", "") or child.get("date", ""),
            child.get("time_window", ""),
            f"진행: {child.get('progress_status', '')}" if child.get("progress_status") else "",
            f"남은 일: {child.get('remaining_work', '')}" if child.get("remaining_work") else "",
            f"확인: {_missing_slots_label(child)}" if child.get("missing_slots") else "",
        )
        if part
    )
    return f"{step_prefix}*{title}*" + (f" — {_escape(detail)}" if detail else "")


def _status_display(item: Mapping[str, str]) -> str:
    icon = {
        "draft": "▫️",
        "posted": "▫️",
        "awaiting_approval": "",
        "approved": "☐",
        "rejected": "⛔",
        "applied": "✅",
        "done": "✅",
    }.get(str(item.get("status") or ""), "▫️")
    status = str(item.get("status_label") or item.get("status") or "")
    return f"{icon} {status}" if icon else status


def _attention_items(sections: Mapping[str, Any]) -> list[Mapping[str, str]]:
    floating = [
        item
        for item in sections["floating"]
        if item.get("missing_slots") or item.get("status") in {"draft", "awaiting_approval", "posted"}
    ]
    floating_by_id = {str(item.get("proposal_id") or ""): item for item in floating}
    pending = []
    for item in sections["pending_approvals"]:
        proposal_id = str(item.get("proposal_id") or "")
        pending.append(
            floating_by_id.get(proposal_id)
            or {
                "proposal_id": proposal_id,
                "title": item["title"],
                "status_label": "승인 대기",
                "missing_slots": "",
                "date": "",
                "date_label": "",
                "is_overdue": "",
                "urgency_label": "",
                "time_window": "",
            }
        )
    seen: set[str] = set()
    merged: list[Mapping[str, str]] = []
    for item in [*pending, *floating]:
        key = str(item.get("proposal_id") or item.get("title") or "")
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return sorted(merged, key=_item_sort_key)


def _attention_lines(items: list[Mapping[str, str]], *, max_items: int) -> list[str]:
    lines = [_proposal_line(item) for item in items[:max_items]]
    if len(items) > max_items:
        lines.append(f"외 {len(items) - max_items}개 더 확인 필요")
    return lines


def _mrkdwn(text: str) -> dict[str, str]:
    return {"type": "mrkdwn", "text": text[:3000]}


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _missing_slots_label(item: Mapping[str, str]) -> str:
    labels = str(item.get("missing_slot_labels") or "")
    if labels:
        return labels
    return render_missing_slot_labels(_split_csv(str(item.get("missing_slots") or "")))


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _item_sort_key(item: Mapping[str, str]) -> tuple[int, str, int, str]:
    return (
        1 if item.get("is_overdue") == "true" else 0,
        str(item.get("date") or "9999-12-31"),
        time_sort_minutes(str(item.get("time_window") or "")),
        str(item.get("title") or ""),
    )
