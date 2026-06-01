from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path


@dataclass(frozen=True)
class SlackCanvasSnapshot:
    actor_id: str
    month: str
    canvas_id: str
    canvas_url: str
    title: str
    created_at: datetime


def load_canvas_snapshots(path: Path) -> tuple[SlackCanvasSnapshot, ...]:
    if not path.exists():
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        SlackCanvasSnapshot(
            actor_id=str(item["actor_id"]),
            month=str(item["month"]),
            canvas_id=str(item["canvas_id"]),
            canvas_url=str(item["canvas_url"]),
            title=str(item["title"]),
            created_at=datetime.fromisoformat(str(item["created_at"])),
        )
        for item in raw
    )


def record_canvas_snapshot(path: Path, snapshot: SlackCanvasSnapshot) -> tuple[SlackCanvasSnapshot, ...]:
    snapshots = [item for item in load_canvas_snapshots(path) if item.canvas_id != snapshot.canvas_id]
    snapshots.append(snapshot)
    snapshots.sort(key=lambda item: (item.month, item.actor_id, item.created_at.isoformat(), item.canvas_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([_snapshot_json(item) for item in snapshots], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return tuple(snapshots)


def latest_canvas_snapshot(
    snapshots: tuple[SlackCanvasSnapshot, ...],
    *,
    actor_id: str,
    month: str,
) -> SlackCanvasSnapshot | None:
    matches = [item for item in snapshots if item.actor_id == actor_id and item.month == month]
    return max(matches, key=lambda item: item.created_at) if matches else None


def render_canvas_snapshot_index(snapshots: tuple[SlackCanvasSnapshot, ...]) -> str:
    if not snapshots:
        return "# Slack Canvas Snapshots\n\n아직 생성된 Canvas snapshot이 없습니다.\n"
    latest_by_key: dict[tuple[str, str], SlackCanvasSnapshot] = {}
    for snapshot in snapshots:
        key = (snapshot.actor_id, snapshot.month)
        if key not in latest_by_key or snapshot.created_at > latest_by_key[key].created_at:
            latest_by_key[key] = snapshot
    lines = [
        "# Slack Canvas Snapshots",
        "",
        "이 파일은 기존 Canvas를 덮어쓰지 않는 snapshot 운영의 최신 링크 기록입니다.",
        "",
        "## Latest by actor/month",
    ]
    for snapshot in sorted(latest_by_key.values(), key=lambda item: (item.month, item.actor_id)):
        lines.append(f"- {snapshot.month} / {snapshot.actor_id}: [{snapshot.title}]({snapshot.canvas_url})")
    lines.extend(["", "## History"])
    for snapshot in snapshots:
        lines.append(
            f"- {snapshot.created_at.isoformat(timespec='seconds')} · {snapshot.month} · {snapshot.actor_id} · "
            f"[{snapshot.title}]({snapshot.canvas_url}) · `{snapshot.canvas_id}`"
        )
    return "\n".join(lines) + "\n"


def _snapshot_json(snapshot: SlackCanvasSnapshot) -> dict[str, str]:
    data = asdict(snapshot)
    data["created_at"] = snapshot.created_at.isoformat(timespec="seconds")
    return data
