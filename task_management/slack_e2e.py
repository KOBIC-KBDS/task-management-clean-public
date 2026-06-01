from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any, Mapping, Sequence
from urllib import parse, request as urlrequest

from .domain import IncomingMessage, OrchestrationResult
from .frontend import build_web_task_page_model, render_web_task_page_html
from .orchestrator import TeamTaskOrchestrator
from .slack_adapter import SlackAdapterError, SlackDmAdapter, SlackDmConfig, SlackHttpClient, dispatch_slack_outbound
from .store import TeamTaskStore


TEST_MARKERS = (
    "[실사용테스트",
    "[E2E_TEST",
    "20개 실사용 시나리오 검증 요약",
    "Task management 운영 점검 요약",
)


@dataclass(frozen=True)
class SlackE2EScenarioMessage:
    scenario_id: str
    phase: str
    text: str
    expected_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlackE2EMessageResult:
    scenario_id: str
    phase: str
    slack_ts: str
    message_id: str
    proposal_count: int
    outbound_count: int
    ignored_duplicate: bool
    titles: tuple[str, ...]
    expected_keywords: tuple[str, ...]
    passed: bool


@dataclass(frozen=True)
class SlackE2ERunResult:
    run_id: str
    checkpoint_dir: str
    dashboard_output: str
    posted_count: int
    processed_count: int
    outbound_count: int
    proposal_count: int
    result_rows: tuple[SlackE2EMessageResult, ...]
    validation_failures: tuple[str, ...] = ()
    summary_ts: str = ""


@dataclass(frozen=True)
class SlackCleanupResult:
    checkpoint_dir: str
    slack_deleted_count: int
    slack_failed: tuple[dict[str, str], ...]
    state_reset: bool
    state_counts_before: dict[str, int]
    state_counts_after: dict[str, int]
    dashboard_output: str


LIVE_E2E_SCENARIOS: tuple[SlackE2EScenarioMessage, ...] = (
    SlackE2EScenarioMessage(
        "S01",
        "create",
        "다음주 중 프로젝트 킥오프 회의를 잡아야 해. 참석자는 나, 김담당 선생님, 이담당 선생님이고 자료 준비도 필요해.",
        ("킥오프",),
    ),
    SlackE2EScenarioMessage("S02", "schedule", "그 킥오프 회의는 다음주 화요일 오후 3시로 하자.", ("킥오프",)),
    SlackE2EScenarioMessage("S03", "location", "킥오프 회의 장소는 2층 회의실 A야.", ("킥오프",)),
    SlackE2EScenarioMessage(
        "S04",
        "progress",
        "킥오프 회의 자료 준비는 절반 정도 끝났고, 남은 건 예산표 정리야.",
        ("킥오프",),
    ),
    SlackE2EScenarioMessage(
        "S05",
        "defer",
        "예산표 정리는 목요일 오전까지 미루고 전날 오후 5시에 다시 알려줘.",
        ("예산표",),
    ),
    SlackE2EScenarioMessage("S06", "complete", "킥오프 회의 준비는 완료했어.", ("킥오프",)),
    SlackE2EScenarioMessage(
        "S07",
        "conflict",
        "수요일 오전 10시에 개인 예약 예약이 있는데 같은 시간 팀 회의가 겹칠 수 있어.",
        ("개인 예약",),
    ),
    SlackE2EScenarioMessage("S08", "resolve_conflict", "개인 예약은 우선 확정하고 팀 회의는 11시로 미뤄줘.", ("개인 예약",)),
    SlackE2EScenarioMessage(
        "S09",
        "routine",
        "매주 월요일 오전 9시 sample-data sync 미팅을 회의실 B에서 하자. 참석자는 나와 김담당 선생님.",
        ("sample-data",),
    ),
    SlackE2EScenarioMessage(
        "S10",
        "subtask",
        "sample-data sync 미팅 전 금요일 오후까지 자료 준비 subtask를 만들고 당일 오전 8시에 리마인드해줘.",
        ("sample-data",),
    ),
    SlackE2EScenarioMessage(
        "S11",
        "shared_decision",
        "이번 주 토요일 Example Lab 워크숍를 가야 해. 오전/오후 중 하루를 정해야 하고 팀원 동의가 필요해.",
        ("Example Lab 워크숍",),
    ),
    SlackE2EScenarioMessage("S12", "fill_slots", "Example Lab 워크숍는 토요일 오후 2시 회의실 A로 하자. 자료 비교가 필요해.", ("Example Lab 워크숍",)),
    SlackE2EScenarioMessage("S13", "complete", "Example Lab 워크숍 입장권 예매는 완료했어.", ("Example Lab 워크숍",)),
    SlackE2EScenarioMessage(
        "S14",
        "external_meeting",
        "ProjectA 담당 이담당 선생님과 금요일 오후 4시에 인터뷰 초안 논의하기.",
        ("인터뷰",),
    ),
    SlackE2EScenarioMessage("S15", "optional_location", "이담당 선생님 인터뷰 논의는 온라인 줌 링크로 할 예정이고 장소는 따로 없어.", ("인터뷰",)),
    SlackE2EScenarioMessage("S16", "progress", "인터뷰 초안은 30% 진행했고 질문지 구성이 아직 남았어.", ("인터뷰",)),
    SlackE2EScenarioMessage(
        "S17",
        "deadline",
        "금요일 오후 5시까지 발표자료 최종본을 보내야 해. 초안은 목요일 밤까지 준비.",
        ("발표자료",),
    ),
    SlackE2EScenarioMessage("S18", "progress", "발표자료 초안은 반만 했고 오늘 밤 11시까지 계속할게.", ("발표자료",)),
    SlackE2EScenarioMessage(
        "S19",
        "delay",
        "발표자료 최종본 발송은 지연될 것 같아. 월요일 오전 10시로 미루고 일요일 저녁에 알려줘.",
        ("발표자료",),
    ),
    SlackE2EScenarioMessage("S20", "complete", "발표자료 최종본 발송 완료했어.", ("발표자료",)),
)


def create_state_checkpoint(state_dir: Path, *, label: str, created_at: datetime) -> Path:
    checkpoint_dir = state_dir / "rollbacks" / f"{created_at.strftime('%Y%m%dT%H%M%S')}-{label}"
    checkpoint_dir.mkdir(parents=True, exist_ok=False)
    copied: list[str] = []
    for name in ("task_management.sqlite3", "events.jsonl"):
        src = state_dir / name
        if src.exists():
            shutil.copy2(src, checkpoint_dir / name)
            copied.append(name)
    manifest = {
        "label": label,
        "created_at": created_at.isoformat(timespec="seconds"),
        "copied": copied,
        "restore_note": "Stop the app, then copy these files back into the state directory to roll back state only.",
    }
    (checkpoint_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return checkpoint_dir


def cleanup_live_slack_e2e(
    *,
    store: TeamTaskStore,
    state_dir: Path,
    adapter: SlackDmAdapter,
    dashboard_output: Path,
    now: datetime,
    reset_state: bool,
    include_db_outbound: bool,
    history_limit: int = 300,
) -> SlackCleanupResult:
    checkpoint = create_state_checkpoint(state_dir, label="pre-e2e-clean", created_at=now)
    counts_before = _state_counts(store.db_path)
    candidate_ts = set(_find_slack_test_message_ts(adapter.config.bot_token, adapter.channel_id, limit=history_limit))
    if include_db_outbound:
        candidate_ts.update(_db_outbound_provider_ts(store.db_path))

    deleted = 0
    failed: list[dict[str, str]] = []
    for ts in sorted(candidate_ts, key=_slack_ts_sort_key, reverse=True):
        try:
            adapter.client.delete_message(adapter.channel_id, ts)
            deleted += 1
            time.sleep(0.15)
        except SlackAdapterError as exc:
            failed.append({"ts": ts, "error": str(exc)})

    if reset_state:
        _reset_state_tables(store.db_path)
        store.append_event(
            "slack.e2e.state_reset",
            {
                "checkpoint_dir": str(checkpoint),
                "deleted_slack_messages": deleted,
                "failed_slack_deletes": len(failed),
                "include_db_outbound": include_db_outbound,
            },
            occurred_at=now,
        )

    _render_dashboard(store, dashboard_output, today=now.date())
    return SlackCleanupResult(
        checkpoint_dir=str(checkpoint),
        slack_deleted_count=deleted,
        slack_failed=tuple(failed),
        state_reset=reset_state,
        state_counts_before=counts_before,
        state_counts_after=_state_counts(store.db_path),
        dashboard_output=str(dashboard_output),
    )


def run_live_slack_e2e(
    *,
    store: TeamTaskStore,
    state_dir: Path,
    orchestrator: TeamTaskOrchestrator,
    adapter: SlackDmAdapter,
    dashboard_output: Path,
    now: datetime,
    run_id: str,
    scenarios: Sequence[SlackE2EScenarioMessage] = LIVE_E2E_SCENARIOS,
    send_inputs: bool,
    send_replies: bool,
    send_summary: bool,
    checkpoint: bool = True,
) -> SlackE2ERunResult:
    checkpoint_dir = (
        create_state_checkpoint(state_dir, label=f"pre-e2e-run-{run_id}", created_at=now) if checkpoint else Path("")
    )
    rows: list[SlackE2EMessageResult] = []
    total_outbound = 0
    posted_count = 0
    for index, scenario in enumerate(scenarios, start=1):
        received_at = now + timedelta(seconds=index)
        text = f"[E2E_TEST {run_id} {scenario.scenario_id}] {scenario.text}"
        slack_ts = ""
        if send_inputs:
            slack_ts = adapter.send_personal(adapter.config.actor_id, text)
            posted_count += 1
            time.sleep(0.2)
        else:
            slack_ts = f"local-{index:02d}"
        message = IncomingMessage(
            message_id=f"slack-e2e/{adapter.channel_id}/{run_id}/{scenario.scenario_id}/{slack_ts}",
            sender_id=adapter.config.actor_id,
            chat_id=adapter.channel_id,
            visibility="private",
            text=text,
            received_at=received_at,
        )
        result = orchestrator.handle_message(message)
        if send_replies and result.outbound_messages:
            dispatch_slack_outbound(store, adapter, result.outbound_messages, sent_at=received_at)
        outbound_count = len(result.outbound_messages)
        total_outbound += outbound_count
        rows.append(_message_result(scenario, slack_ts=slack_ts, message_id=message.message_id, result=result))
        store.append_event(
            "slack.e2e.scenario.processed",
            {
                "run_id": run_id,
                "scenario_id": scenario.scenario_id,
                "phase": scenario.phase,
                "slack_ts": slack_ts,
                "proposal_count": len(result.proposals),
                "outbound_count": outbound_count,
                "send_inputs": send_inputs,
                "send_replies": send_replies,
            },
            occurred_at=received_at,
        )

    _render_dashboard(store, dashboard_output, today=now.date())
    proposal_count = len(store.list_proposals())
    summary_ts = ""
    if send_summary:
        summary = _render_e2e_summary(run_id=run_id, rows=rows, proposal_count=proposal_count, dashboard_output=dashboard_output)
        summary_ts = adapter.send_personal(adapter.config.actor_id, summary)
    failures = tuple(row.scenario_id for row in rows if not row.passed)
    store.append_event(
        "slack.e2e.run.completed",
        {
            "run_id": run_id,
            "posted_count": posted_count,
            "processed_count": len(rows),
            "outbound_count": total_outbound,
            "proposal_count": proposal_count,
            "validation_failures": list(failures),
            "dashboard_output": str(dashboard_output),
            "summary_ts": summary_ts,
        },
        occurred_at=now + timedelta(seconds=len(rows) + 1),
    )
    return SlackE2ERunResult(
        run_id=run_id,
        checkpoint_dir=str(checkpoint_dir) if checkpoint else "",
        dashboard_output=str(dashboard_output),
        posted_count=posted_count,
        processed_count=len(rows),
        outbound_count=total_outbound,
        proposal_count=proposal_count,
        result_rows=tuple(rows),
        validation_failures=failures,
        summary_ts=summary_ts,
    )


def _message_result(
    scenario: SlackE2EScenarioMessage,
    *,
    slack_ts: str,
    message_id: str,
    result: OrchestrationResult,
) -> SlackE2EMessageResult:
    result_titles = tuple(proposal.title for proposal in result.proposals)
    evidence_parts: list[str] = [*result_titles]
    evidence_parts.extend(
        json.dumps(proposal.metadata, ensure_ascii=False, sort_keys=True)
        for proposal in result.proposals
        if proposal.metadata
    )
    evidence_parts.extend(message.text for message in result.outbound_messages)
    evidence_parts.extend(
        json.dumps(message.card, ensure_ascii=False, sort_keys=True)
        for message in result.outbound_messages
        if message.card
    )
    haystack = " ".join(evidence_parts).lower()
    passed = not scenario.expected_keywords or any(keyword.lower() in haystack for keyword in scenario.expected_keywords)
    return SlackE2EMessageResult(
        scenario_id=scenario.scenario_id,
        phase=scenario.phase,
        slack_ts=slack_ts,
        message_id=message_id,
        proposal_count=len(result.proposals),
        outbound_count=len(result.outbound_messages),
        ignored_duplicate=result.ignored_duplicate,
        titles=result_titles,
        expected_keywords=scenario.expected_keywords,
        passed=passed,
    )


def _render_e2e_summary(
    *,
    run_id: str,
    rows: Sequence[SlackE2EMessageResult],
    proposal_count: int,
    dashboard_output: Path,
) -> str:
    lines = [
        f"🧪 20개 실사용 시나리오 검증 요약 ({run_id})",
        f"- Slack DM 테스트 입력: {len(rows)}개",
        f"- 현재 운영 상태 proposal: {proposal_count}개",
        f"- 검증 실패: {', '.join(row.scenario_id for row in rows if not row.passed) or '없음'}",
        f"- 대시보드: {dashboard_output}",
        "",
        "처리 요약",
    ]
    for row in rows:
        title = ", ".join(row.titles) if row.titles else "신규 proposal 없음/기존 항목 업데이트"
        lines.append(f"- {row.scenario_id} {row.phase}: proposal {row.proposal_count}, 답변 {row.outbound_count} · {title}")
    return "\n".join(lines)


def _render_dashboard(store: TeamTaskStore, dashboard_output: Path, *, today) -> None:
    dashboard_output.parent.mkdir(parents=True, exist_ok=True)
    model = build_web_task_page_model(store, today=today)
    dashboard_output.write_text(render_web_task_page_html(model), encoding="utf-8")


def _find_slack_test_message_ts(token: str, channel_id: str, *, limit: int) -> tuple[str, ...]:
    if not token:
        raise SlackAdapterError("SLACK_BOT_TOKEN is required to find Slack test messages")
    found: list[str] = []
    cursor = ""
    remaining = limit
    while remaining > 0:
        page_limit = min(200, remaining)
        query = {"channel": channel_id, "limit": str(page_limit)}
        if cursor:
            query["cursor"] = cursor
        req = urlrequest.Request(
            f"https://slack.com/api/conversations.history?{parse.urlencode(query)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urlrequest.urlopen(req, timeout=20) as response:  # nosec - explicit Slack live operation
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise SlackAdapterError(str(payload.get("error") or payload))
        for message in payload.get("messages") or ():
            text = str(message.get("text") or "")
            ts = str(message.get("ts") or "")
            if ts and any(marker in text for marker in TEST_MARKERS):
                found.append(ts)
        cursor = str(((payload.get("response_metadata") or {}).get("next_cursor")) or "")
        if not cursor:
            break
        remaining -= page_limit
    return tuple(found)


def _db_outbound_provider_ts(db_path: Path) -> tuple[str, ...]:
    if not db_path.exists():
        return ()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            select provider_message_id
            from outbound_deliveries
            where provider = 'slack' and provider_message_id != ''
            """
        ).fetchall()
    return tuple(str(row[0]) for row in rows if row[0])


def _reset_state_tables(db_path: Path) -> None:
    if not db_path.exists():
        return
    tables = (
        "applied_exports",
        "approval_decisions",
        "approval_requests",
        "inbound_event_queue",
        "integration_state",
        "messages",
        "outbound_deliveries",
        "outbound_message_queue",
        "proposals",
    )
    with sqlite3.connect(db_path) as conn:
        for table in tables:
            conn.execute(f"delete from {table}")
        conn.commit()


def _state_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    counts: dict[str, int] = {}
    with sqlite3.connect(db_path) as conn:
        tables = [
            row[0]
            for row in conn.execute("select name from sqlite_master where type='table' order by name").fetchall()
        ]
        for table in tables:
            counts[table] = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
    return counts


def _slack_ts_sort_key(ts: str) -> tuple[int, int]:
    left, _, right = ts.partition(".")
    try:
        return int(left), int(right or "0")
    except ValueError:
        return (0, 0)


def result_to_dict(result: SlackE2ERunResult | SlackCleanupResult) -> dict[str, Any]:
    return asdict(result)
