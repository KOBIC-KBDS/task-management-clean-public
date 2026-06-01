from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from task_management.store import TeamTaskStore


def test_store_migrates_legacy_household_area_column(tmp_path: Path) -> None:
    db_path = tmp_path / "task_management.sqlite3"
    event_log_path = tmp_path / "events.jsonl"

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table proposals (
                proposal_id text primary key,
                source_message_id text not null,
                proposer_id text not null,
                title text not null,
                raw_text text not null,
                kind text not null,
                status text not null,
                assigned_to text not null,
                household_area text not null,
                discussion_id text not null,
                message_id text not null,
                required_approvers text not null,
                approvals text not null,
                missing_slots text not null,
                due_date text,
                scheduled_date text,
                time_window text not null,
                source_url text not null,
                source_export_path text not null,
                created_at text,
                updated_at text,
                metadata text not null
            )
            """
        )
        conn.execute(
            """
            insert into proposals(
                proposal_id, source_message_id, proposer_id, title, raw_text, kind, status,
                assigned_to, household_area, discussion_id, message_id, required_approvers,
                approvals, missing_slots, due_date, scheduled_date, time_window, source_url,
                source_export_path, created_at, updated_at, metadata
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "proposal/legacy",
                "message/legacy",
                "me",
                "legacy task",
                "legacy task",
                "task",
                "approved",
                "me",
                "work",
                "",
                "message/legacy",
                json.dumps(["me"]),
                json.dumps(["me"]),
                json.dumps([]),
                "2026-05-26",
                None,
                "",
                "",
                "",
                "2026-05-26T08:00:00",
                "2026-05-26T08:00:00",
                json.dumps({}),
            ),
        )

    store = TeamTaskStore(db_path, event_log_path)

    proposals = store.list_proposals()
    assert len(proposals) == 1
    assert proposals[0].task_management_area == "work"

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(proposals)").fetchall()}
    assert "household_area" in columns
    assert "task_management_area" in columns
