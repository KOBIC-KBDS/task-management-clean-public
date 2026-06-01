from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from task_management.discussion_adapter import parse_manual_discussion
from task_management.task_core_bridge import build_task_management_task_export, validate_with_task_core


def test_builds_task_core_export_v1_preview_payload() -> None:
    candidates = parse_manual_discussion(
        """나: 이번 주 금요일까지 보고서 자료 챙겨줘.
팀원: 내가 공유폴더에 챙길게. 당신은 서류 확인해줘.
나: 고객 미팅 예약은 다음주 화요일 오전으로 잡아줘.
""",
        discussion_id="manual/2026-05-05",
        reference_date=date(2026, 5, 5),
        source_export_path="exports/수동-대화.txt",
    )

    payload = build_task_management_task_export(
        candidates,
        exported_at=datetime(2026, 5, 5, 10, 0, 0),
    )

    assert payload["schema_version"] == "task-core.export.v1"
    assert payload["sync_model"] == "task_management-preview-only-adapter"
    assert payload["diagnostics"]["mutates_files"] is False
    assert payload["diagnostics"]["utf8_required"] is True
    assert payload["diagnostics"]["korean_path_supported"] is True
    assert len(payload["items"]) >= 3

    first = payload["items"][0]
    assert first["metadata"]["source_adapter"] == "task_management"
    assert first["metadata"]["source_adapter_schema"] == "task-task_management.discussion-adapter.v1"
    assert first["metadata"]["discussion_id"] == "manual/2026-05-05"
    assert first["metadata"]["source_export_path"] == "exports/수동-대화.txt"
    assert first["path"] == "manual"

    assigned = {item["metadata"]["assigned_to"] for item in payload["items"]}
    areas = {item["metadata"]["task_management_area"] for item in payload["items"]}
    assert {"me", "teammate"} <= assigned
    assert {"work", "health"} <= areas


def test_payload_validates_with_task_core_without_restore(tmp_path: Path) -> None:
    candidates = parse_manual_discussion(
        "나: 내일까지 보고서 확인해줘.",
        discussion_id="manual/2026-05-05",
        reference_date=date(2026, 5, 5),
    )
    payload = build_task_management_task_export(
        candidates,
        exported_at=datetime(2026, 5, 5, 10, 0, 0),
    )

    preview = validate_with_task_core(payload, root=tmp_path)

    assert preview["ok"] is True
    assert preview["restores"] is False
    assert preview["would_create"] == len(payload["items"])
    assert preview["would_update"] == 0


def test_date_window_metadata_matches_task_core_r023_contract(tmp_path: Path) -> None:
    candidates = parse_manual_discussion(
        "나: 워크숍 이번주 목~일 중 하루 가야함",
        discussion_id="manual/2026-05-05",
        reference_date=date(2026, 5, 5),
    )
    payload = build_task_management_task_export(
        candidates,
        exported_at=datetime(2026, 5, 5, 10, 0, 0),
    )

    item = payload["items"][0]
    assert "exact_date" in item["missing_slots"]
    assert item["metadata"]["date_window_start"] == "2026-05-07"
    assert item["metadata"]["date_window_end"] == "2026-05-10"
    assert item["metadata"]["date_window_label"] == "이번주 목~일"
    assert item["metadata"]["needs_exact_date"] == "true"

    preview = validate_with_task_core(payload, root=tmp_path)

    assert preview["ok"] is True
    assert preview["restores"] is False
    assert preview["issues"] == []
    convention = preview["accepted_metadata_conventions"]["date_window"]
    assert convention["policy"] == "metadata-first; top-level mirrors are accepted for transition but not required"
    assert convention["item_count"] == 1
    assert convention["needs_exact_date_count"] == 1
    assert convention["missing_slots"] == ["exact_date"]
