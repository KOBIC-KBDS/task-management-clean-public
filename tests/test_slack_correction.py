from __future__ import annotations

import json
from pathlib import Path

import pytest

from task_management.cli import main
from task_management.store import TeamTaskStore


class _FakeCorrectionSlackClient:
    instances: list["_FakeCorrectionSlackClient"] = []

    def __init__(self, token: str) -> None:
        self.token = token
        self.deleted: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str, str]] = []
        _FakeCorrectionSlackClient.instances.append(self)

    def open_dm(self, user_id: str) -> str:
        return "DOPENED"

    def read_channel(self, channel_id: str, *, oldest: str = "", limit: int = 100) -> tuple[dict[str, str], ...]:
        return ()

    def send_message(self, channel_id: str, text: str) -> str:
        ts = f"3000.{len(self.sent) + 1:06d}"
        self.sent.append((channel_id, ts, text))
        return ts

    def delete_message(self, channel_id: str, ts: str) -> None:
        self.deleted.append((channel_id, ts))


def test_cli_slack_correct_recalls_bad_messages_and_sends_utf8_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeCorrectionSlackClient.instances = []
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_DM_CHANNEL_ID", "DTEST")
    monkeypatch.setenv("TASK_MANAGEMENT_SLACK_ACTOR_ID", "me")
    monkeypatch.setattr("task_management.cli.SlackHttpClient", _FakeCorrectionSlackClient)
    text_file = tmp_path / "correction.md"
    text_file.write_text(
        "정리 보정했습니다.\n\n*DataPortal / 김담당 선생님*\n• trajectory 분석 알고리즘 설정\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"

    main(
        [
            "--state",
            str(state),
            "slack-correct",
            "--delete-ts",
            "111.000001",
            "--delete-ts",
            "222.000002",
            "--text-file",
            str(text_file),
            "--verify-contains",
            "정리 보정했습니다",
            "--verify-contains",
            "김담당",
            "--dedupe-key",
            "test/correction",
            "--send",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output["deleted_ts"] == ["111.000001", "222.000002"]
    assert output["sent"] is True
    assert output["provider_message_id"] == "3000.000001"
    assert "xoxb-test" not in json.dumps(output)
    client = _FakeCorrectionSlackClient.instances[0]
    assert client.deleted == [("DTEST", "111.000001"), ("DTEST", "222.000002")]
    assert client.sent == [
        (
            "DTEST",
            "3000.000001",
            "정리 보정했습니다.\n\n*DataPortal / 김담당 선생님*\n• trajectory 분석 알고리즘 설정",
        )
    ]

    store = TeamTaskStore(state / "task_management.sqlite3", state / "events.jsonl")
    assert store.has_outbound_delivery("test/correction")
    assert [event["type"] for event in store.read_events()] == [
        "slack.message.deleted",
        "slack.message.deleted",
        "slack.message.sent",
    ]


def test_cli_slack_correct_dry_run_has_no_external_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeCorrectionSlackClient.instances = []
    monkeypatch.setattr("task_management.cli.SlackHttpClient", _FakeCorrectionSlackClient)
    text_file = tmp_path / "correction.md"
    text_file.write_text("정리 보정했습니다.", encoding="utf-8")

    main(
        [
            "--state",
            str(tmp_path / "state"),
            "slack-correct",
            "--delete-ts",
            "111.000001",
            "--text-file",
            str(text_file),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output["send"] is False
    assert output["would_delete_ts"] == ["111.000001"]
    assert output["safety"]["external_side_effects"] is False
    assert _FakeCorrectionSlackClient.instances == []


def test_cli_slack_correct_blocks_send_when_verify_text_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeCorrectionSlackClient.instances = []
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_DM_CHANNEL_ID", "DTEST")
    monkeypatch.setattr("task_management.cli.SlackHttpClient", _FakeCorrectionSlackClient)
    text_file = tmp_path / "correction.md"
    text_file.write_text("정리 보정했습니다.", encoding="utf-8")

    with pytest.raises(SystemExit, match="missing required verify text"):
        main(
            [
                "--state",
                str(tmp_path / "state"),
                "slack-correct",
                "--delete-ts",
                "111.000001",
                "--text-file",
                str(text_file),
                "--verify-contains",
                "김담당",
                "--send",
            ]
        )

    assert _FakeCorrectionSlackClient.instances == []
