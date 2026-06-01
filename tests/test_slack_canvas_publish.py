from __future__ import annotations

from datetime import date, datetime
import json

from task_management.cli import main
from task_management.domain import Proposal
from task_management.slack_canvas import build_canvas_replace_payload
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 18, 9, 0, 0)


def test_build_canvas_replace_payload_uses_whole_canvas_replace() -> None:
    payload = build_canvas_replace_payload("# 2026년 5월 개인 Task Management\n")

    assert payload == {
        "changes": [
            {
                "operation": "replace",
                "document_content": {
                    "type": "markdown",
                    "markdown": "# 2026년 5월 개인 Task Management\n",
                },
            }
        ]
    }


def test_publish_slack_monthly_page_cli_is_dry_run_by_default(tmp_path) -> None:
    state = tmp_path / "state"
    store = TeamTaskStore(state / "task_management.sqlite3", state / "events.jsonl")
    store.save_proposal(
        Proposal(
            proposal_id="proposal/committee",
            source_message_id="slack/DTEST/committee",
            proposer_id="me",
            title="리뷰위원회 발표 배석",
            raw_text="리뷰위원회 발표 배석",
            kind="event",
            status="approved",
            assigned_to="me",
            task_management_area="general",
            discussion_id="slack/DTEST",
            message_id="slack/DTEST/committee",
            required_approvers=("me",),
            approvals=("me",),
            scheduled_date=date(2026, 5, 18),
            time_window="16:30",
            created_at=NOW,
            updated_at=NOW,
            metadata={"participant_label": "나/김센터 센터장님"},
        )
    )
    output = tmp_path / "out" / "page.md"
    payload_output = tmp_path / "out" / "payload.json"

    main(
        [
            "--state",
            str(state),
            "publish-slack-monthly-page",
            "--month",
            "2026-05",
            "--actor",
            "me",
            "--canvas-id",
            "FTEST",
            "--output",
            str(output),
            "--payload-output",
            str(payload_output),
        ]
    )

    markdown = output.read_text(encoding="utf-8")
    payload = json.loads(payload_output.read_text(encoding="utf-8"))
    assert "리뷰위원회 발표 배석" in markdown
    assert payload["canvas_id"] == "FTEST"
    assert payload["changes"][0]["operation"] == "replace"
    assert "2026년 5월 개인 Task Management" in payload["changes"][0]["document_content"]["markdown"]
