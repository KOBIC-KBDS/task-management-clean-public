from __future__ import annotations

from datetime import datetime
from pathlib import Path

from task_management.operating_agent import RuleBasedTeamTaskOperatingAgent
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.slack_adapter import FakeSlackWebClient, SlackDmAdapter, SlackDmConfig
from task_management.slack_e2e import (
    LIVE_E2E_SCENARIOS,
    cleanup_live_slack_e2e,
    run_live_slack_e2e,
)
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 20, 9, 0, 0)


def _store(root: Path) -> TeamTaskStore:
    return TeamTaskStore(root / "task_management.sqlite3", root / "events.jsonl")


def _adapter() -> tuple[SlackDmAdapter, FakeSlackWebClient]:
    client = FakeSlackWebClient(channel_id="DTEST")
    config = SlackDmConfig(actor_id="me", dm_channel_id="DTEST", bot_token="xoxb-test")
    return SlackDmAdapter(config, client), client


def test_live_e2e_scenario_catalog_has_twenty_ordered_messages() -> None:
    assert len(LIVE_E2E_SCENARIOS) == 20
    assert LIVE_E2E_SCENARIOS[0].scenario_id == "S01"
    assert LIVE_E2E_SCENARIOS[-1].phase == "complete"
    assert any(item.phase == "progress" for item in LIVE_E2E_SCENARIOS)
    assert any(item.phase == "defer" for item in LIVE_E2E_SCENARIOS)


def test_run_live_slack_e2e_posts_marked_inputs_and_renders_dashboard(tmp_path: Path) -> None:
    store = _store(tmp_path)
    adapter, client = _adapter()
    result = run_live_slack_e2e(
        store=store,
        state_dir=tmp_path,
        orchestrator=TeamTaskOrchestrator(store, operating_agent=RuleBasedTeamTaskOperatingAgent()),
        adapter=adapter,
        dashboard_output=tmp_path / "dashboard.html",
        now=NOW,
        run_id="pytest",
        send_inputs=True,
        send_replies=False,
        send_summary=True,
    )

    assert result.posted_count == 20
    assert result.processed_count == 20
    assert len(client.sent) == 21
    assert "[E2E_TEST pytest S01]" in client.sent[0][2]
    assert "20개 실사용 시나리오 검증 요약" in client.sent[-1][2]
    assert (tmp_path / "dashboard.html").exists()
    assert result.checkpoint_dir


def test_cleanup_checkpoints_deletes_known_slack_ts_and_resets_state(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.record_outbound_delivery(
        dedupe_key="old-test",
        surface="personal_chat",
        recipient_id="me",
        provider="slack",
        provider_message_id="111.222",
        sent_at=NOW,
        payload={"text": "old"},
    )
    adapter, client = _adapter()

    monkeypatch.setattr("task_management.slack_e2e._find_slack_test_message_ts", lambda *args, **kwargs: ("333.444",))
    result = cleanup_live_slack_e2e(
        store=store,
        state_dir=tmp_path,
        adapter=adapter,
        dashboard_output=tmp_path / "dashboard.html",
        now=NOW,
        reset_state=True,
        include_db_outbound=True,
    )

    assert sorted(ts for _, ts in client.deleted) == ["111.222", "333.444"]
    assert result.slack_deleted_count == 2
    assert result.state_counts_before["outbound_deliveries"] == 1
    assert result.state_counts_after["outbound_deliveries"] == 0
    assert Path(result.checkpoint_dir, "task_management.sqlite3").exists()
    assert (tmp_path / "dashboard.html").exists()
