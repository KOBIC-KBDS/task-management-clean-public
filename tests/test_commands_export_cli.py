from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path

import pytest

from task_management.chat_adapter import dispatch_outbound
from task_management.cli import main
from task_management.commands import parse_chat_command
from task_management.export_service import export_and_mark_approved_proposals_applied
from task_management.simulator import TeamTaskSimulator


NOW = datetime(2026, 5, 5, 10, 0, 0)


def _clear_local_loader_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_DM_CHANNEL_ID",
        "SLACK_USER_ID",
        "TASK_MANAGEMENT_INSTANCE_ID",
        "TASK_MANAGEMENT_ALLOWED_INSTANCE_ID",
        "TASK_MANAGEMENT_STATE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_chat_command_parser_supports_korean_reply_commands() -> None:
    accepted = parse_chat_command("수락 approval/abc123")
    rejected = parse_chat_command("거절 approval/abc123")
    rejected_code = parse_chat_command("`거절 approval/abc123`")
    rejected_bold = parse_chat_command("*거절 approval/abc123*")
    changed = parse_chat_command("변경 approval/abc123 다음주 수요일 오후")
    completed = parse_chat_command("완료 task_management/abc123")
    assigned = parse_chat_command("담당 task_management/abc123 팀원")

    assert accepted is not None
    assert accepted.action == "accept"
    assert accepted.target_id == "approval/abc123"
    assert rejected is not None
    assert rejected.action == "reject"
    assert rejected_code is not None
    assert rejected_code.action == "reject"
    assert rejected_bold is not None
    assert rejected_bold.action == "reject"
    assert changed is not None
    assert changed.action == "change"
    assert changed.body == "다음주 수요일 오후"
    assert completed is not None
    assert completed.action == "complete"
    assert assigned is not None
    assert assigned.action == "assign"
    assert assigned.body == "팀원"


def test_kakao_style_accept_reply_approves_pending_request(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    created = sim.send_private(
        "me",
        "팀원이 다음주 화요일 견적서 확인하면 좋겠어",
        message_id="dm/me/command-target",
        received_at=NOW,
    )
    request_id = created.approval_requests[0].request_id

    accepted = sim.send_private(
        "teammate",
        f"수락 {request_id}",
        message_id="dm/teammate/accept-command",
        received_at=NOW.replace(hour=10, minute=3),
    )

    assert accepted.proposals[0].status == "approved"
    assert accepted.proposals[0].approvals == ("teammate",)
    assert accepted.outbound_messages[0].surface == "team_room"
    assert [event["type"] for event in sim.store.read_events()] == [
        "message.received",
        "agent.decision.created",
        "proposal.created",
        "approval.requested",
        "message.received",
        "approval.accepted",
        "proposal.approved",
    ]


def test_change_reply_resolves_date_window_and_approves_question(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    created = sim.send_private(
        "me",
        "워크숍 이번주 목~일 중 하루 가야함",
        message_id="dm/me/change-date-window",
        received_at=NOW,
    )
    request_id = created.approval_requests[0].request_id

    changed = sim.send_private(
        "me",
        f"변경 {request_id} 이번주 토요일 오후 나랑 팀원 같이",
        message_id="dm/me/change-command",
        received_at=NOW.replace(hour=10, minute=4),
    )

    proposal = changed.proposals[0]
    assert proposal.status == "approved"
    assert proposal.kind == "event"
    assert proposal.scheduled_date.isoformat() == "2026-05-09"
    assert proposal.time_window == "afternoon"
    assert proposal.missing_slots == ()
    assert "date_window_start" not in proposal.metadata
    assert proposal.metadata["resolved_date_window_start"] == "2026-05-07"
    assert proposal.metadata["resolved_date_window_end"] == "2026-05-10"
    assert changed.approval_requests[0].status == "accepted"
    assert changed.outbound_messages[0].surface == "team_room"

    exported = export_and_mark_approved_proposals_applied(
        sim.store,
        exported_at=NOW.replace(hour=10, minute=6),
        task_core_root=tmp_path / "task-core-root",
    )
    assert exported.preview["ok"] is True
    assert exported.payload["items"][0]["metadata"]["scheduled_for"] == "2026-05-09"
    assert exported.payload["items"][0]["metadata"]["time_window"] == "afternoon"
    assert exported.applied_proposal_ids == (proposal.proposal_id,)


def test_assign_command_reassigns_pending_proposal_and_requests_assignee(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    created = sim.send_private(
        "me",
        "견적서 확인해야 해",
        message_id="dm/me/assign-source",
        received_at=NOW,
    )
    proposal_id = created.proposals[0].proposal_id

    assigned = sim.send_private(
        "me",
        f"담당 {proposal_id} 팀원",
        message_id="dm/me/assign-command",
        received_at=NOW.replace(hour=10, minute=7),
    )

    proposal = assigned.proposals[0]
    assert proposal.assigned_to == "teammate"
    assert proposal.status == "awaiting_approval"
    assert proposal.missing_slots == ("date",)
    assert assigned.approval_requests[0].approver_id == "me"
    assert assigned.outbound_messages[0].surface == "personal_chat"
    assert "proposal.assigned" in [event["type"] for event in sim.store.read_events()]


def test_complete_command_marks_proposal_done_and_notifies_team(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    proposal = sim.send_private(
        "me",
        "내가 내일 보고서 확인할게",
        message_id="dm/me/complete-source",
        received_at=NOW,
    ).proposals[0]

    completed = sim.send_private(
        "me",
        f"완료 {proposal.proposal_id}",
        message_id="dm/me/complete-command",
        received_at=NOW.replace(hour=11),
    )

    updated = completed.proposals[0]
    assert updated.status == "done"
    assert updated.metadata["completed_by"] == "me"
    assert updated.metadata["completed_at"] == "2026-05-05T11:00:00"
    assert completed.outbound_messages[0].surface == "team_room"
    assert "proposal.completed" in [event["type"] for event in sim.store.read_events()]


def test_export_service_marks_approved_proposals_applied_after_valid_preview(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    proposal = sim.send_private(
        "me",
        "내가 내일 보고서 확인할게",
        message_id="dm/me/export-apply",
        received_at=NOW,
    ).proposals[0]

    result = export_and_mark_approved_proposals_applied(
        sim.store,
        exported_at=NOW.replace(hour=10, minute=10),
        task_core_root=tmp_path / "task-core-root",
    )

    assert result.preview["ok"] is True
    assert result.preview["restores"] is False
    assert result.applied_proposal_ids == (proposal.proposal_id,)
    assert sim.store.get_proposal(proposal.proposal_id).status == "applied"
    assert "proposal.applied" in [event["type"] for event in sim.store.read_events()]


def test_cli_renders_dashboard_and_exports_preview_json(tmp_path: Path) -> None:
    state = tmp_path / "state"
    output_html = tmp_path / "dashboard.html"
    output_json = tmp_path / "preview.json"
    sim = TeamTaskSimulator(state)
    sim.send_private(
        "me",
        "내가 내일 보고서 확인할게",
        message_id="dm/me/cli-approved",
        received_at=NOW,
    )

    main(["--state", str(state), "render-dashboard", "--today", "2026-05-05", "--output", str(output_html)])
    main(
        [
            "--state",
            str(state),
            "export-preview",
            "--task-core-root",
            str(tmp_path / "task-core-root"),
            "--output",
            str(output_json),
        ]
    )

    assert output_html.exists()
    assert "TeamTask Task Page" in output_html.read_text(encoding="utf-8")
    exported = json.loads(output_json.read_text(encoding="utf-8"))
    assert exported["preview"]["ok"] is True
    assert exported["payload"]["items"][0]["metadata"]["proposal_id"]


def test_cli_demo_runs_credential_free_flow_and_writes_outputs(tmp_path: Path) -> None:
    state = tmp_path / "demo-state"
    output_dir = tmp_path / "demo-out"
    fixture = Path("examples/slack_dogfood_demo.json")

    main(
        [
            "--state",
            str(state),
            "demo",
            "--fixture",
            str(fixture),
            "--output-dir",
            str(output_dir),
            "--task-core-root",
            str(tmp_path / "task-core-root"),
        ]
    )

    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    preview = json.loads((output_dir / "task-core-preview.json").read_text(encoding="utf-8"))
    dashboard = (output_dir / "dashboard.html").read_text(encoding="utf-8")
    outbox = (output_dir / "simulated-dm-outbox.md").read_text(encoding="utf-8")

    assert summary["preview"] == {"ok": True, "restores": False, "mutates_files": False}
    assert summary["counts"]["preview_items"] >= 3
    assert preview["preview"]["ok"] is True
    assert preview["preview"]["restores"] is False
    assert preview["payload"]["diagnostics"]["mutates_files"] is False
    assert "워크숍 가기" in dashboard
    assert "sample-data sync 미팅" in dashboard
    assert "Task-core preview" in dashboard
    assert "정확한 날짜" in outbox
    assert "확정했습니다" in outbox


def test_cli_demo_is_rerunnable_with_same_state_directory(tmp_path: Path) -> None:
    state = tmp_path / "demo-state"
    output_dir = tmp_path / "demo-out"
    fixture = Path("examples/slack_dogfood_demo.json")
    argv = [
        "--state",
        str(state),
        "demo",
        "--fixture",
        str(fixture),
        "--output-dir",
        str(output_dir),
        "--task-core-root",
        str(tmp_path / "task-core-root"),
    ]

    main(argv)
    first_outbox = (output_dir / "simulated-dm-outbox.md").read_text(encoding="utf-8")
    main(argv)

    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    second_outbox = (output_dir / "simulated-dm-outbox.md").read_text(encoding="utf-8")
    assert summary["preview"] == {"ok": True, "restores": False, "mutates_files": False}
    assert summary["counts"]["preview_items"] >= 3
    assert summary["counts"]["outbound_messages"] >= 1
    assert summary["counts"]["current_run_outbound_messages"] == 0
    assert second_outbox == first_outbox
    assert "정확한 날짜" in second_outbox


def test_cli_dogfood_loop_runs_private_transcript_cycle_and_respects_stop_file(tmp_path: Path, capsys) -> None:
    state = tmp_path / "dogfood-state"
    dashboard = tmp_path / "dashboard.html"
    transcript = tmp_path / "slack-transcript.json"
    transcript.write_text(
        json.dumps(
            [
                {
                    "channel": "DTEST",
                    "ts": "1779101000.000001",
                    "user": "UUSER",
                    "text": "워크숍 이번주 목~일 중 하루 가야함",
                    "received_at": "2026-05-05T09:00:00",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    main(
        [
            "--state",
            str(state),
            "dogfood-loop",
            "--transcript-input",
            str(transcript),
            "--dashboard-output",
            str(dashboard),
            "--interval-seconds",
            "0",
            "--max-cycles",
            "1",
            "--send",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "private-user-only-slack-dm"
    assert result["cycles"] == 1
    assert result["safety"] == {
        "kakao_live": False,
        "public_deploy": False,
        "task_core_writes": False,
        "slack_scope": "personal_dm_only",
    }
    assert len(result["fake_sent"]) == 1
    assert dashboard.exists()
    assert "워크숍 가기" in dashboard.read_text(encoding="utf-8")

    stop_file = state / "STOP"
    stop_file.write_text("stop\n", encoding="utf-8")
    main(
        [
            "--state",
            str(state),
            "dogfood-loop",
            "--transcript-input",
            str(transcript),
            "--dashboard-output",
            str(dashboard),
            "--interval-seconds",
            "0",
            "--max-cycles",
            "1",
        ]
    )
    stopped = json.loads(capsys.readouterr().out)
    assert stopped["cycles"] == 0
    assert stopped["stopped_by_stop_file"] is True


def test_cli_dogfood_loop_rejects_zero_interval_without_cycle_cap(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="requires --max-cycles"):
        main(
            [
                "--state",
                str(tmp_path / "state"),
                "dogfood-loop",
                "--transcript-input",
                "examples/slack_transcript_demo.json",
                "--interval-seconds",
                "0",
            ]
        )


def test_cli_slack_doctor_reports_ready_env_without_token_leak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _clear_local_loader_env(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_DM_CHANNEL_ID", "DTEST")
    monkeypatch.setenv("SLACK_BOT_USER_ID", "U_BOT")

    main(["--state", str(tmp_path / "state"), "slack-doctor"])

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["can_poll"] is True
    assert result["can_send"] is True
    assert result["token_kind"] == "bot"
    assert result["required_bot_scopes"] == ["chat:write", "im:history", "im:write", "reactions:write"]
    assert result["live_open_dm"] == {"attempted": False}
    assert result["safety"]["slack_scope"] == "personal_dm_only"
    assert "xoxb-test" not in json.dumps(result)


def test_cli_loads_env_local_and_overrides_inherited_live_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_local_loader_env(monkeypatch)
    (tmp_path / ".env.local").write_text(
        "\n".join(
            [
                "SLACK_BOT_TOKEN=xoxb-demo",
                "SLACK_DM_CHANNEL_ID=DDEMO",
                "TASK_MANAGEMENT_INSTANCE_ID=clean-demo",
                "TASK_MANAGEMENT_ALLOWED_INSTANCE_ID=clean-demo",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-live")
    monkeypatch.setenv("SLACK_DM_CHANNEL_ID", "DPROD")

    main(["--state", str(tmp_path / "state"), "slack-doctor"])

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["can_send"] is True
    assert result["instance_id"] == "clean-demo"
    assert result["allowed_instance_id"] == "clean-demo"
    assert result["instance_guard_ok"] is True
    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-demo"
    assert os.environ["SLACK_DM_CHANNEL_ID"] == "DDEMO"
    assert "xoxb-live" not in json.dumps(result)
    assert "xoxb-demo" not in json.dumps(result)


def test_blank_env_local_values_clear_inherited_slack_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_local_loader_env(monkeypatch)
    (tmp_path / ".env.local").write_text(
        "\n".join(
            [
                "SLACK_BOT_TOKEN=",
                "SLACK_APP_TOKEN=",
                "SLACK_DM_CHANNEL_ID=",
                "SLACK_USER_ID=",
                "TASK_MANAGEMENT_INSTANCE_ID=clean-demo",
                "TASK_MANAGEMENT_ALLOWED_INSTANCE_ID=clean-demo",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-live")
    monkeypatch.setenv("SLACK_DM_CHANNEL_ID", "DPROD")
    monkeypatch.setenv("SLACK_USER_ID", "UPROD")

    with pytest.raises(SystemExit, match="not ready"):
        main(["--state", str(tmp_path / "state"), "slack-doctor", "--strict"])

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["can_send"] is False
    assert any("SLACK_BOT_TOKEN" in error for error in result["errors"])
    assert os.environ["SLACK_BOT_TOKEN"] == ""
    assert os.environ["SLACK_DM_CHANNEL_ID"] == ""
    assert os.environ["SLACK_USER_ID"] == ""


def test_cli_loads_markdown_env_table_after_blank_env_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_local_loader_env(monkeypatch)
    (tmp_path / ".env.local").write_text(
        "\n".join(
            [
                "SLACK_BOT_TOKEN=",
                "SLACK_DM_CHANNEL_ID=",
                "TASK_MANAGEMENT_INSTANCE_ID=clean-demo",
                "TASK_MANAGEMENT_ALLOWED_INSTANCE_ID=clean-demo",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env.local.md").write_text(
        "\n".join(
            [
                "# Local values",
                "",
                "| Key | Value |",
                "| --- | --- |",
                "| SLACK_BOT_TOKEN | xoxb-demo |",
                "| SLACK_DM_CHANNEL_ID | DDEMO |",
                "| TASK_MANAGEMENT_STATE | .state-md |",
                "| TASK_MANAGEMENT_INSTANCE_ID | clean-demo-md |",
                "| TASK_MANAGEMENT_ALLOWED_INSTANCE_ID | clean-demo-md |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-live")
    monkeypatch.setenv("SLACK_DM_CHANNEL_ID", "DPROD")

    main(["slack-doctor"])

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["can_send"] is True
    assert result["instance_id"] == "clean-demo-md"
    assert result["allowed_instance_id"] == "clean-demo-md"
    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-demo"
    assert os.environ["SLACK_DM_CHANNEL_ID"] == "DDEMO"
    assert (tmp_path / ".state-md" / "task_management.sqlite3").exists()


def test_cli_slack_doctor_strict_exits_when_env_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_DM_CHANNEL_ID", raising=False)
    monkeypatch.delenv("SLACK_USER_ID", raising=False)

    with pytest.raises(SystemExit, match="not ready"):
        main(["--state", str(tmp_path / "state"), "slack-doctor", "--strict"])

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert any("SLACK_BOT_TOKEN" in error for error in result["errors"])
    assert result["safety"]["sends_messages"] is False


def test_cli_slack_doctor_live_open_dm_rejects_non_personal_dm_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    _clear_local_loader_env(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SLACK_DM_CHANNEL_ID", raising=False)
    monkeypatch.setenv("SLACK_USER_ID", "UUSER")

    class FakeHttpClient:
        def __init__(self, token: str) -> None:
            assert token == "xoxb-test"

        def open_dm(self, user_id: str) -> str:
            assert user_id == "UUSER"
            return "GMPIM"

    monkeypatch.setattr("task_management.cli.SlackHttpClient", FakeHttpClient)

    main(["--state", str(tmp_path / "state"), "slack-doctor", "--live-open-dm"])

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["live_open_dm"] == {
        "attempted": True,
        "ok": False,
        "dm_channel_id": "GMPIM",
        "source": "conversations.open",
        "error": "resolved channel is not a personal D... DM id",
    }
    assert "xoxb-test" not in json.dumps(result)


def test_cli_slack_doctor_strict_fails_when_live_open_dm_resolution_is_not_personal_dm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    _clear_local_loader_env(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SLACK_DM_CHANNEL_ID", raising=False)
    monkeypatch.setenv("SLACK_USER_ID", "UUSER")

    class FakeHttpClient:
        def __init__(self, token: str) -> None:
            assert token == "xoxb-test"

        def open_dm(self, user_id: str) -> str:
            assert user_id == "UUSER"
            return "GMPIM"

    monkeypatch.setattr("task_management.cli.SlackHttpClient", FakeHttpClient)

    with pytest.raises(SystemExit, match="DM resolution is not ready"):
        main(["--state", str(tmp_path / "state"), "slack-doctor", "--live-open-dm", "--strict"])

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["live_open_dm"]["ok"] is False
    assert "xoxb-test" not in json.dumps(result)


def test_cli_imports_kakao_export_text(tmp_path: Path) -> None:
    state = tmp_path / "state"
    export_path = tmp_path / "kakao.txt"
    export_path.write_text(
        """--------------- 2026년 5월 5일 화요일 ---------------
[나] [오전 8:55] 워크숍 이번주 목~일 중 하루 가야함
[나] [오전 9:01] 내가 내일 보고서 확인할게
""",
        encoding="utf-8",
    )

    main(
        [
            "--state",
            str(state),
            "import-kakao",
            "--input",
            str(export_path),
            "--alias",
            "나=me",
        ]
    )

    sim = TeamTaskSimulator(state)
    proposals = sim.store.list_proposals()
    assert len(proposals) == 2
    assert {proposal.status for proposal in proposals} == {"approved", "awaiting_approval"}


def test_chat_adapter_dispatch_keeps_future_kakao_wrapper_thin(tmp_path: Path) -> None:
    sim = TeamTaskSimulator(tmp_path)
    result = sim.send_private(
        "me",
        "팀원이 다음주 화요일 견적서 확인하면 좋겠어",
        message_id="dm/me/dispatch",
        received_at=NOW,
    )
    adapter = RecordingAdapter()

    dispatch_outbound(adapter, result.outbound_messages)

    assert adapter.personal == [("teammate", result.outbound_messages[0].text)]
    assert adapter.team == []


class RecordingAdapter:
    def __init__(self) -> None:
        self.personal: list[tuple[str, str]] = []
        self.team: list[str] = []

    def poll_messages(self):
        return ()

    def send_personal(self, actor_id: str, text: str) -> None:
        self.personal.append((actor_id, text))

    def send_team(self, text: str) -> None:
        self.team.append(text)
