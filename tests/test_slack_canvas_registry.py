from __future__ import annotations

from datetime import datetime

from task_management.cli import main
from task_management.slack_canvas_registry import (
    SlackCanvasSnapshot,
    latest_canvas_snapshot,
    load_canvas_snapshots,
    record_canvas_snapshot,
    render_canvas_snapshot_index,
)


def test_canvas_snapshot_registry_tracks_latest_by_month(tmp_path) -> None:
    registry = tmp_path / "snapshots.json"
    old = SlackCanvasSnapshot(
        actor_id="me",
        month="2026-05",
        canvas_id="FOLD",
        canvas_url="https://example.com/old",
        title="old",
        created_at=datetime(2026, 5, 18, 9, 0, 0),
    )
    new = SlackCanvasSnapshot(
        actor_id="me",
        month="2026-05",
        canvas_id="FNEW",
        canvas_url="https://example.com/new",
        title="new",
        created_at=datetime(2026, 5, 18, 10, 0, 0),
    )

    record_canvas_snapshot(registry, old)
    snapshots = record_canvas_snapshot(registry, new)

    loaded = load_canvas_snapshots(registry)
    latest = latest_canvas_snapshot(snapshots, actor_id="me", month="2026-05")
    index = render_canvas_snapshot_index(loaded)
    assert len(loaded) == 2
    assert latest is not None
    assert latest.canvas_id == "FNEW"
    assert "[new](https://example.com/new)" in index
    assert "`FOLD`" in index


def test_record_slack_canvas_snapshot_cli_writes_registry_and_index(tmp_path) -> None:
    registry = tmp_path / "out" / "snapshots.json"
    index = tmp_path / "out" / "snapshots.md"

    main(
        [
            "record-slack-canvas-snapshot",
            "--registry",
            str(registry),
            "--index-output",
            str(index),
            "--month",
            "2026-05",
            "--actor",
            "me",
            "--canvas-id",
            "CANVAS_SNAPSHOT_ID",
            "--canvas-url",
            "https://example.invalid/docs/canvas-snapshot-id",
            "--title",
            "2026년 5월 개인 Task Management",
            "--created-at",
            "2026-05-18T10:00:00",
        ]
    )

    assert "CANVAS_SNAPSHOT_ID" in registry.read_text(encoding="utf-8")
    assert "2026년 5월 개인 Task Management" in index.read_text(encoding="utf-8")
