from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import date, datetime
import json
import os
from pathlib import Path
import time
from typing import Sequence

from .backfill_report import build_workflow_backfill_report, write_workflow_backfill_report
from .claude_code_operating_agent import ClaudeCodeCliOperatingAgent
from .codex_operating_agent import CodexCliOperatingAgent
from .domain import OrchestrationResult
from .export_service import export_and_mark_approved_proposals_applied, preview_approved_proposals
from .frontend import build_web_task_page_model, render_web_task_page_html
from .kakao_export_adapter import parse_kakao_text_export
from .openai_operating_agent import OpenAIResponsesOperatingAgent
from .operating_agent import TeamTaskOperatingAgent, RuleBasedTeamTaskOperatingAgent
from .orchestrator import TeamTaskOrchestrator
from .reminders import build_due_reminders
from .secretary import (
    build_afternoon_briefing,
    build_end_of_day_review,
    build_morning_briefing,
    build_proactive_checks,
)
from .simulator import TeamTaskSimulator
from .slack_adapter import (
    FakeSlackWebClient,
    SlackDmAdapter,
    SlackDmConfig,
    SlackAdapterError,
    SlackHttpClient,
    diagnose_slack_live_config,
    dispatch_slack_outbound,
    is_slack_personal_dm_channel_id,
    run_slack_dm_once,
)
from .slack_canvas import (
    SlackCanvasConfig,
    SlackCanvasError,
    SlackCanvasHttpClient,
    build_canvas_replace_payload,
)
from .slack_canvas_registry import (
    SlackCanvasSnapshot,
    latest_canvas_snapshot,
    load_canvas_snapshots,
    record_canvas_snapshot,
    render_canvas_snapshot_index,
)
from .slack_correction import recall_and_send_slack_correction
from .slack_cycle import run_slack_task_cycle
from .slack_e2e import cleanup_live_slack_e2e, result_to_dict, run_live_slack_e2e
from .slack_fast_cycle import run_slack_fast_cycle
from .slack_page import build_slack_monthly_task_page_model, render_slack_monthly_task_page_markdown
from .slack_socket import (
    FakeSlackSocketClient,
    SlackSocketConfig,
    SlackSocketError,
    diagnose_slack_socket_config,
    run_slack_socket_loop,
)
from .store import TeamTaskStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Team task orchestration utilities.")
    parser.add_argument("--state", type=Path, default=Path(".task-management"), help="State directory for SQLite/JSONL.")
    parser.add_argument(
        "--agent",
        choices=["auto", "rule", "codex", "claude", "openai"],
        default=os.environ.get("TASK_MANAGEMENT_OPERATING_AGENT", "auto"),
        help=(
            "Operating agent implementation. Default: TASK_MANAGEMENT_OPERATING_AGENT or auto "
            "(Codex for live Slack paths, rule for local fixtures; set claude for Claude Code login sessions)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    render = subparsers.add_parser("render-dashboard", help="Render the web task page as static HTML.")
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--today", default=date.today().isoformat())

    slack_page = subparsers.add_parser(
        "render-slack-monthly-page",
        help="Render a Slack personal monthly task page as Markdown.",
    )
    slack_page.add_argument("--output", type=Path, required=True)
    slack_page.add_argument("--month", default=date.today().strftime("%Y-%m"), help="Target month as YYYY-MM.")
    slack_page.add_argument("--actor", default="me", help="Personal actor id for the Slack page.")

    slack_canvas = subparsers.add_parser(
        "publish-slack-monthly-page",
        help="Render the Slack personal monthly page and optionally replace a Slack Canvas.",
    )
    slack_canvas.add_argument("--output", type=Path, required=True, help="Local Markdown copy to write first.")
    slack_canvas.add_argument("--month", default=date.today().strftime("%Y-%m"), help="Target month as YYYY-MM.")
    slack_canvas.add_argument("--actor", default="me", help="Personal actor id for the Slack page.")
    slack_canvas.add_argument("--canvas-id", default=os.environ.get("SLACK_CANVAS_ID", ""))
    slack_canvas.add_argument("--send", action="store_true", help="Actually call Slack canvases.edit.")
    slack_canvas.add_argument(
        "--payload-output",
        type=Path,
        help="Write the Slack canvases.edit JSON payload for inspection.",
    )

    registry = subparsers.add_parser(
        "record-slack-canvas-snapshot",
        help="Record a newly created Slack snapshot Canvas link locally.",
    )
    registry.add_argument("--registry", type=Path, default=Path("out/slack-canvas-snapshots.json"))
    registry.add_argument("--index-output", type=Path, default=Path("out/slack-canvas-snapshots.md"))
    registry.add_argument("--month", required=True, help="Target month as YYYY-MM.")
    registry.add_argument("--actor", default="me")
    registry.add_argument("--canvas-id", required=True)
    registry.add_argument("--canvas-url", required=True)
    registry.add_argument("--title", required=True)
    registry.add_argument("--created-at", default=datetime.now().isoformat(timespec="seconds"))

    subparsers.add_parser("proposals", help="Print proposals as JSON.")
    subparsers.add_parser("pending", help="Print pending approval requests as JSON.")
    subparsers.add_parser("events", help="Print audit events as JSON.")

    export = subparsers.add_parser("export-preview", help="Export approved proposals as task-core preview JSON.")
    export.add_argument("--output", type=Path)
    export.add_argument("--mark-applied", action="store_true", help="Mark locally approved proposals as applied if preview is valid.")
    export.add_argument("--task-core-root", type=Path)

    backfill = subparsers.add_parser(
        "workflow-backfill-preview",
        help="Write read-only workflow hierarchy backfill suggestions without mutating state.",
    )
    backfill.add_argument("--output", type=Path, required=True)

    simulate = subparsers.add_parser("simulate", help="Run fixture actions through the simulator.")
    simulate.add_argument("--fixture", type=Path, required=True)

    demo = subparsers.add_parser(
        "demo",
        help="Run a credential-free dogfood demo, render dashboard/monthly page, and validate task-core preview.",
    )
    demo.add_argument("--fixture", type=Path, default=Path("examples/slack_dogfood_demo.json"))
    demo.add_argument("--output-dir", type=Path, default=Path("out/demo"))
    demo.add_argument("--today", default="2026-05-05")
    demo.add_argument("--month", default="2026-05")
    demo.add_argument("--actor", default="me")
    demo.add_argument("--task-core-root", type=Path)

    kakao = subparsers.add_parser("import-kakao", help="Import a local KakaoTalk text export into orchestration state.")
    kakao.add_argument("--input", type=Path, required=True)
    kakao.add_argument("--alias", action="append", default=[], help="Sender alias mapping like ??me. Repeatable.")
    kakao.add_argument("--chat-id", default="kakao/family")
    kakao.add_argument("--visibility", choices=["private", "family"], default="family")

    slack_poll = subparsers.add_parser("slack-poll", help="Poll the configured Slack personal DM once.")
    slack_poll.add_argument("--once", action="store_true", help="Run one poll cycle. Present for future interval compatibility.")
    slack_poll.add_argument("--send", action="store_true", help="Actually send outbound Slack DM responses.")

    slack_doctor = subparsers.add_parser(
        "slack-doctor",
        help="Check Slack live Web API personal-DM environment without sending messages.",
    )
    slack_doctor.add_argument("--strict", action="store_true", help="Exit non-zero when required Slack env is missing/unsafe.")
    slack_doctor.add_argument(
        "--live-open-dm",
        action="store_true",
        help="Actually call conversations.open to resolve SLACK_USER_ID into a D... DM id. Sends no messages.",
    )

    slack_correct = subparsers.add_parser(
        "slack-correct",
        help="Recall bad bot DM replies and send one UTF-8 correction message.",
    )
    slack_correct.add_argument(
        "--delete-ts",
        action="append",
        default=[],
        help="Slack message ts to delete. Repeat for each bad bot reply.",
    )
    slack_correct.add_argument(
        "--text-file",
        type=Path,
        required=True,
        help="UTF-8 correction message file. Prefer this over shell inline text for Korean.",
    )
    slack_correct.add_argument("--actor", default=os.environ.get("TASK_MANAGEMENT_SLACK_ACTOR_ID", "me"))
    slack_correct.add_argument("--reason", default="manual correction after user feedback")
    slack_correct.add_argument("--dedupe-key", default="")
    slack_correct.add_argument(
        "--verify-contains",
        action="append",
        default=[],
        help="Required substring that must exist in the correction text before sending. Repeatable.",
    )
    slack_correct.add_argument("--force", action="store_true", help="Send even if the dedupe key was already delivered.")
    slack_correct.add_argument("--send", action="store_true", help="Actually delete Slack messages and send the correction.")

    slack_socket_doctor = subparsers.add_parser(
        "slack-socket-doctor",
        help="Check Slack Socket Mode env without opening a WebSocket.",
    )
    slack_socket_doctor.add_argument("--strict", action="store_true", help="Exit non-zero when Socket Mode env is missing/unsafe.")

    slack_socket_loop = subparsers.add_parser(
        "slack-socket-loop",
        help="Listen to Slack Socket Mode message.im events and run the fast task intake path.",
    )
    slack_socket_loop.add_argument("--transcript-input", type=Path, help="Use local Socket Mode envelopes instead of live Slack.")
    slack_socket_loop.add_argument("--send", action="store_true", help="Actually send direct Slack DM responses.")
    slack_socket_loop.add_argument("--actor", default="me")
    slack_socket_loop.add_argument("--max-events", type=int, default=0, help="0 means run until stopped/disconnected.")
    slack_socket_loop.add_argument("--max-seconds", type=float, default=0.0, help="0 means no wall-clock limit.")
    slack_socket_loop.add_argument("--no-reconnect", action="store_true", help="Do not reconnect after Slack requests refresh.")
    slack_socket_loop.add_argument("--dashboard-output", type=Path, default=Path("out/dashboard.html"))
    slack_socket_loop.add_argument("--home-dashboard-url", default=os.environ.get("TASK_MANAGEMENT_DASHBOARD_URL", ""))
    slack_socket_loop.add_argument(
        "--watch-channel-id",
        action="append",
        default=[],
        help="Allowlist one Slack D/C/G channel for mention-only notification triage. Repeatable.",
    )
    slack_socket_loop.add_argument(
        "--watch-all-allowed-messages",
        action="store_true",
        help="For allowlisted channels, process all messages instead of only messages mentioning SLACK_USER_ID.",
    )

    slack_fast_cycle = subparsers.add_parser(
        "slack-fast-cycle",
        help="Fast path: poll Slack DM, apply semantic/core decisions, render the web task page.",
    )
    slack_fast_cycle.add_argument("--transcript-input", type=Path, help="Use a local Slack JSON transcript instead of live Slack.")
    slack_fast_cycle.add_argument("--send", action="store_true", help="Actually send direct Slack DM responses.")
    slack_fast_cycle.add_argument("--actor", default="me")
    slack_fast_cycle.add_argument("--now", default=datetime.now().isoformat(timespec="seconds"))
    slack_fast_cycle.add_argument("--dashboard-output", type=Path, default=Path("out/dashboard.html"))
    slack_fast_cycle.add_argument("--home-dashboard-url", default=os.environ.get("TASK_MANAGEMENT_DASHBOARD_URL", ""))

    dogfood_loop = subparsers.add_parser(
        "dogfood-loop",
        help="Run the private user-only Slack DM dogfood loop until a stop file or max cycles.",
    )
    dogfood_loop.add_argument("--transcript-input", type=Path, help="Use a local Slack JSON transcript instead of live Slack.")
    dogfood_loop.add_argument("--send", action="store_true", help="Actually send direct Slack DM responses/digests.")
    dogfood_loop.add_argument("--actor", default="me")
    dogfood_loop.add_argument("--interval-seconds", type=float, default=300.0)
    dogfood_loop.add_argument("--max-cycles", type=int, default=0, help="0 means run until the stop file appears.")
    dogfood_loop.add_argument("--stop-file", type=Path, help="Default: <state>/STOP")
    dogfood_loop.add_argument("--dashboard-output", type=Path, default=Path("out/dashboard.html"))

    slack_cycle = subparsers.add_parser(
        "slack-task-cycle",
        help="Poll Slack DM, reconcile task state, render web/monthly pages, and optionally send a DM digest.",
    )
    slack_cycle.add_argument("--transcript-input", type=Path, help="Use a local Slack JSON transcript instead of live Slack.")
    slack_cycle.add_argument("--send", action="store_true", help="Actually send the task cycle DM digest.")
    slack_cycle.add_argument("--actor", default="me")
    slack_cycle.add_argument("--month", default=date.today().strftime("%Y-%m"), help="Target month as YYYY-MM.")
    slack_cycle.add_argument("--now", default=datetime.now().isoformat(timespec="seconds"))
    slack_cycle.add_argument("--dashboard-output", type=Path, default=Path("out/dashboard.html"))
    slack_cycle.add_argument("--monthly-output", type=Path, default=Path("out/slack-monthly-task-page.md"))
    slack_cycle.add_argument("--digest-output", type=Path, default=Path("out/slack-task-cycle-digest.md"))
    slack_cycle.add_argument("--dashboard-url", default="http://127.0.0.1:8787/dashboard.html")
    slack_cycle.add_argument("--canvas-url", default="")

    transcript = subparsers.add_parser("slack-transcript", help="Run a local Slack JSON transcript through the DM adapter.")
    transcript.add_argument("--input", type=Path, required=True)
    transcript.add_argument("--send", action="store_true", help="Record/send via fake outbox instead of dry-run only.")

    reminders = subparsers.add_parser("reminders", help="Render due Slack DM reminders.")
    reminders.add_argument("--now", default=datetime.now().isoformat(timespec="seconds"))
    reminders.add_argument("--send", action="store_true", help="Send reminders through configured Slack DM.")

    briefing = subparsers.add_parser(
        "morning-briefing",
        help="Render the proactive secretary morning briefing for Slack DM.",
    )
    briefing.add_argument("--now", default=datetime.now().isoformat(timespec="seconds"))
    briefing.add_argument("--actor", default="me")
    briefing.add_argument("--dashboard-url", default="http://127.0.0.1:8787/dashboard.html")
    briefing.add_argument("--send", action="store_true", help="Send the briefing through configured Slack DM.")

    afternoon = subparsers.add_parser(
        "afternoon-briefing",
        help="Render the proactive secretary afternoon briefing for Slack DM.",
    )
    afternoon.add_argument("--now", default=datetime.now().isoformat(timespec="seconds"))
    afternoon.add_argument("--actor", default="me")
    afternoon.add_argument("--dashboard-url", default="http://127.0.0.1:8787/dashboard.html")
    afternoon.add_argument("--send", action="store_true", help="Send the briefing through configured Slack DM.")

    proactive_checks = subparsers.add_parser(
        "proactive-checks",
        help="Ask about due unfinished work through Slack DM.",
    )
    proactive_checks.add_argument("--now", default=datetime.now().isoformat(timespec="seconds"))
    proactive_checks.add_argument("--actor", default="me")
    proactive_checks.add_argument("--send", action="store_true", help="Send checks through configured Slack DM.")

    eod_review = subparsers.add_parser(
        "end-of-day-review",
        help="Ask for progress on today's unfinished work before it silently rolls over.",
    )
    eod_review.add_argument("--now", default=datetime.now().isoformat(timespec="seconds"))
    eod_review.add_argument("--actor", default="me")
    eod_review.add_argument("--send", action="store_true", help="Send the review through configured Slack DM.")

    e2e_clean = subparsers.add_parser(
        "slack-e2e-clean",
        help="Checkpoint state, recall Slack test messages, clear operating test legacy, and render dashboard.",
    )
    e2e_clean.add_argument("--now", default=datetime.now().isoformat(timespec="seconds"))
    e2e_clean.add_argument("--dashboard-output", type=Path, default=Path("out/dashboard.html"))
    e2e_clean.add_argument("--history-limit", type=int, default=300)
    e2e_clean.add_argument("--keep-state", action="store_true", help="Only recall Slack messages; do not reset local state.")
    e2e_clean.add_argument(
        "--skip-db-outbound",
        action="store_true",
        help="Do not delete Slack ts values recorded in outbound_deliveries.",
    )

    e2e_run = subparsers.add_parser(
        "slack-e2e-run",
        help="Post/process the 20 live Slack dogfood scenarios and render dashboard.",
    )
    e2e_run.add_argument("--now", default=datetime.now().isoformat(timespec="seconds"))
    e2e_run.add_argument("--run-id", default="")
    e2e_run.add_argument("--dashboard-output", type=Path, default=Path("out/dashboard.html"))
    e2e_run.add_argument("--send-inputs", action="store_true", help="Post marked scenario inputs into the Slack DM.")
    e2e_run.add_argument("--send-replies", action="store_true", help="Actually send task-management replies to Slack.")
    e2e_run.add_argument("--send-summary", action="store_true", help="Send one Slack summary message after the run.")
    e2e_run.add_argument("--no-checkpoint", action="store_true", help="Skip the pre-run rollback checkpoint.")

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.agent = _resolve_operating_agent_name(
        args.agent,
        command=args.command,
        transcript_input=getattr(args, "transcript_input", None),
    )
    store = _store(args.state)

    if args.command == "render-dashboard":
        model = build_web_task_page_model(store, today=date.fromisoformat(args.today))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_web_task_page_html(model), encoding="utf-8")
        print(str(args.output))
    elif args.command == "render-slack-monthly-page":
        month = date.fromisoformat(f"{args.month}-01")
        model = build_slack_monthly_task_page_model(store, actor_id=args.actor, month=month)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_slack_monthly_task_page_markdown(model), encoding="utf-8")
        print(str(args.output))
    elif args.command == "publish-slack-monthly-page":
        month = date.fromisoformat(f"{args.month}-01")
        model = build_slack_monthly_task_page_model(store, actor_id=args.actor, month=month)
        markdown = render_slack_monthly_task_page_markdown(model)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(markdown, encoding="utf-8")
        payload = {"canvas_id": args.canvas_id, **build_canvas_replace_payload(markdown)}
        if args.payload_output:
            args.payload_output.parent.mkdir(parents=True, exist_ok=True)
            args.payload_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.send:
            config = SlackCanvasConfig.from_env()
            canvas_id = args.canvas_id or config.canvas_id
            try:
                response = SlackCanvasHttpClient(config.token).replace_canvas(canvas_id, markdown)
            except SlackCanvasError as exc:
                raise SystemExit(f"Slack Canvas not configured: {exc}") from exc
            _print_json({"output": str(args.output), "canvas_id": canvas_id, "response": response})
        else:
            _print_json(
                {
                    "output": str(args.output),
                    "payload_output": str(args.payload_output) if args.payload_output else "",
                    "canvas_id": args.canvas_id,
                    "send": False,
                    "next": "Review the Markdown/payload, then rerun with --send and a token bearing canvases:write.",
                }
            )
    elif args.command == "record-slack-canvas-snapshot":
        snapshot = SlackCanvasSnapshot(
            actor_id=args.actor,
            month=args.month,
            canvas_id=args.canvas_id,
            canvas_url=args.canvas_url,
            title=args.title,
            created_at=datetime.fromisoformat(args.created_at),
        )
        snapshots = record_canvas_snapshot(args.registry, snapshot)
        args.index_output.parent.mkdir(parents=True, exist_ok=True)
        args.index_output.write_text(render_canvas_snapshot_index(snapshots), encoding="utf-8")
        latest = latest_canvas_snapshot(snapshots, actor_id=args.actor, month=args.month)
        _print_json(
            {
                "registry": str(args.registry),
                "index_output": str(args.index_output),
                "latest_canvas_id": latest.canvas_id if latest else "",
                "latest_canvas_url": latest.canvas_url if latest else "",
                "snapshot_count": len(snapshots),
            }
        )
    elif args.command == "proposals":
        _print_json([asdict(item) for item in store.list_proposals()])
    elif args.command == "pending":
        _print_json([asdict(item) for item in store.list_approval_requests(status="pending")])
    elif args.command == "events":
        _print_json(list(store.read_events()))
    elif args.command == "export-preview":
        exported_at = datetime.now()
        if args.mark_applied:
            result = export_and_mark_approved_proposals_applied(
                store,
                exported_at=exported_at,
                task_core_root=args.task_core_root,
            )
        else:
            result = preview_approved_proposals(
                store,
                exported_at=exported_at,
                task_core_root=args.task_core_root,
            )
        rendered = json.dumps(
            {
                "payload": result.payload,
                "preview": result.preview,
                "applied_proposal_ids": list(result.applied_proposal_ids),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
            print(str(args.output))
        else:
            print(rendered)
    elif args.command == "workflow-backfill-preview":
        report = build_workflow_backfill_report(store.list_proposals(), store.read_events())
        output = write_workflow_backfill_report(report, args.output)
        _print_json({"output": str(output), "counts": report["counts"], "mutation_policy": report["mutation_policy"]})
    elif args.command == "simulate":
        simulator = TeamTaskSimulator(args.state, operating_agent=_build_operating_agent(args.agent))
        actions = json.loads(args.fixture.read_text(encoding="utf-8"))
        results = simulator.run_fixture(actions)
        _print_json([asdict(item) for item in results])
    elif args.command == "demo":
        summary = _run_demo(args)
        _print_json(summary)
    elif args.command == "import-kakao":
        simulator = TeamTaskSimulator(args.state, operating_agent=_build_operating_agent(args.agent))
        messages = parse_kakao_text_export(
            args.input.read_text(encoding="utf-8"),
            actor_aliases=_aliases(args.alias),
            chat_id=args.chat_id,
            visibility=args.visibility,
        )
        results = [simulator.orchestrator.handle_message(message) for message in messages]
        _print_json(
            {
                "message_count": len(messages),
                "result_count": len(results),
                "results": [asdict(item) for item in results],
            }
        )
    elif args.command == "slack-transcript":
        config = SlackDmConfig(dm_channel_id="DTEST")
        raw_messages = json.loads(args.input.read_text(encoding="utf-8-sig"))
        client = FakeSlackWebClient(messages=list(raw_messages), channel_id=config.dm_channel_id)
        adapter = SlackDmAdapter(
            config,
            client,
            oldest=store.get_integration_state("slack.dm.me.last_ts") or "",
        )
        result = run_slack_dm_once(
            store=store,
            orchestrator=TeamTaskOrchestrator(store, operating_agent=_build_operating_agent(args.agent)),
            adapter=adapter,
            send=args.send,
            now=datetime.now(),
        )
        _print_json(
            {
                "message_count": len(result.messages),
                "result_count": len(result.results),
                "outbound_messages": [asdict(item) for item in result.outbound_messages],
                "fake_sent": client.sent,
            }
        )
    elif args.command == "slack-poll":
        config = SlackDmConfig.from_env()
        try:
            adapter = SlackDmAdapter(
                config,
                oldest=store.get_integration_state(f"slack.dm.{config.actor_id}.last_ts") or "",
            )
            result = run_slack_dm_once(
                store=store,
                orchestrator=TeamTaskOrchestrator(store, operating_agent=_build_operating_agent(args.agent)),
                adapter=adapter,
                send=args.send,
                now=datetime.now(),
            )
        except SlackAdapterError as exc:
            raise SystemExit(f"Slack adapter not configured: {exc}") from exc
        _print_json(
            {
                "message_count": len(result.messages),
                "result_count": len(result.results),
                "outbound_count": len(result.outbound_messages),
                "send": args.send,
            }
        )
    elif args.command == "slack-doctor":
        config = SlackDmConfig.from_env()
        check = diagnose_slack_live_config(config)
        socket_check = diagnose_slack_socket_config(SlackSocketConfig.from_env())
        payload: dict[str, object] = {
            "ok": check.ok,
            "can_poll": check.can_poll,
            "can_send": check.can_send,
            "actor_id": check.actor_id,
            "token_env": check.token_env,
            "token_kind": check.token_kind,
            "bot_token_set": check.bot_token_set,
            "dm_channel_id_set": check.dm_channel_id_set,
            "user_id_set": check.user_id_set,
            "bot_user_id_set": check.bot_user_id_set,
            "dm_resolution": check.dm_resolution,
            "required_bot_scopes": list(check.required_bot_scopes),
            "instance_id": check.instance_id,
            "allowed_instance_id": check.allowed_instance_id,
            "instance_guard_ok": check.instance_guard_ok,
            "errors": list(check.errors),
            "warnings": list(check.warnings),
            "setup_steps": list(check.setup_steps),
            "live_open_dm": {"attempted": False},
            "socket_mode": _socket_check_payload(socket_check),
            "safety": {
                "slack_scope": "personal_dm_plus_allowlisted_notifications"
                if socket_check.watched_channel_ids
                else "personal_dm_only",
                "sends_messages": False,
                "prints_token": False,
                "task_core_writes": False,
                "allowlisted_notification_confirmation_required": True,
            },
        }
        if args.live_open_dm:
            live_result: dict[str, object] = {"attempted": True, "ok": False}
            if not check.ok:
                live_result["error"] = "local_config_not_ready"
            elif config.dm_channel_id:
                live_result.update({"ok": True, "dm_channel_id": config.dm_channel_id, "source": "SLACK_DM_CHANNEL_ID"})
            else:
                try:
                    dm_channel_id = SlackHttpClient(config.bot_token).open_dm(config.user_id)
                except SlackAdapterError as exc:
                    live_result["error"] = str(exc)
                else:
                    if is_slack_personal_dm_channel_id(dm_channel_id):
                        live_result.update({"ok": True, "dm_channel_id": dm_channel_id, "source": "conversations.open"})
                    else:
                        live_result.update(
                            {
                                "ok": False,
                                "dm_channel_id": dm_channel_id,
                                "source": "conversations.open",
                                "error": "resolved channel is not a personal D... DM id",
                            }
                        )
            payload["live_open_dm"] = live_result
        _print_json(payload)
        if args.strict and not check.ok:
            raise SystemExit("Slack live Web API environment is not ready")
        live_open_dm = payload["live_open_dm"]
        if args.strict and args.live_open_dm and isinstance(live_open_dm, dict) and not live_open_dm.get("ok"):
            raise SystemExit("Slack live Web API DM resolution is not ready")
    elif args.command == "slack-correct":
        correction_text = args.text_file.read_text(encoding="utf-8").strip()
        missing = [needle for needle in args.verify_contains if needle not in correction_text]
        if missing:
            raise SystemExit(f"correction text missing required verify text: {missing}")
        if not args.send:
            _print_json(
                {
                    "send": False,
                    "would_delete_ts": list(args.delete_ts),
                    "text_file": str(args.text_file),
                    "text_length": len(correction_text),
                    "verify_contains": list(args.verify_contains),
                    "safety": {
                        "external_side_effects": False,
                        "rerun_with": "--send",
                        "encoding": "text_file_utf8",
                    },
                }
            )
        else:
            config = SlackDmConfig.from_env()
            config = SlackDmConfig(
                actor_id=args.actor,
                user_id=config.user_id,
                dm_channel_id=config.dm_channel_id,
                bot_user_id=config.bot_user_id,
                bot_token=config.bot_token,
                chat_id=config.chat_id,
            )
            result = recall_and_send_slack_correction(
                store=store,
                adapter=SlackDmAdapter(config, SlackHttpClient(config.bot_token)),
                delete_ts=tuple(args.delete_ts),
                correction_text=correction_text,
                actor_id=args.actor,
                reason=args.reason,
                dedupe_key=args.dedupe_key,
                verify_contains=tuple(args.verify_contains),
                force=args.force,
            )
            _print_json(asdict(result))
    elif args.command == "slack-socket-doctor":
        socket_check = diagnose_slack_socket_config(SlackSocketConfig.from_env())
        payload = _socket_check_payload(socket_check)
        payload["safety"] = {
            "slack_scope": "personal_dm_plus_allowlisted_notifications"
            if socket_check.watched_channel_ids
            else "personal_dm_only",
            "sends_messages": False,
            "prints_token": False,
            "task_core_writes": False,
            "allowlisted_notification_confirmation_required": True,
        }
        _print_json(payload)
        if args.strict and not socket_check.ok:
            raise SystemExit("Slack Socket Mode environment is not ready")
    elif args.command == "slack-socket-loop":
        socket_config = SlackSocketConfig.from_env()
        fake_web_client = None
        if args.actor != socket_config.dm_config.actor_id:
            socket_config = SlackSocketConfig(
                app_token=socket_config.app_token,
                dm_config=SlackDmConfig(
                    actor_id=args.actor,
                    user_id=socket_config.dm_config.user_id,
                    dm_channel_id=socket_config.dm_config.dm_channel_id,
                    bot_user_id=socket_config.dm_config.bot_user_id,
                    bot_token=socket_config.dm_config.bot_token,
                    chat_id=socket_config.dm_config.chat_id,
                    instance_id=socket_config.dm_config.instance_id,
                    allowed_instance_id=socket_config.dm_config.allowed_instance_id,
                    watched_channel_ids=socket_config.dm_config.watched_channel_ids,
                    watch_requires_mention=socket_config.dm_config.watch_requires_mention,
                ),
            )
        if args.watch_channel_id or args.watch_all_allowed_messages:
            dm_config = socket_config.dm_config
            socket_config = SlackSocketConfig(
                app_token=socket_config.app_token,
                dm_config=SlackDmConfig(
                    actor_id=dm_config.actor_id,
                    user_id=dm_config.user_id,
                    dm_channel_id=dm_config.dm_channel_id,
                    bot_user_id=dm_config.bot_user_id,
                    bot_token=dm_config.bot_token,
                    chat_id=dm_config.chat_id,
                    instance_id=dm_config.instance_id,
                    allowed_instance_id=dm_config.allowed_instance_id,
                    watched_channel_ids=tuple(
                        dict.fromkeys((*dm_config.watched_channel_ids, *tuple(args.watch_channel_id or ())))
                    ),
                    watch_requires_mention=False
                    if args.watch_all_allowed_messages
                    else dm_config.watch_requires_mention,
                ),
            )
        client = None
        if args.transcript_input:
            envelopes = json.loads(args.transcript_input.read_text(encoding="utf-8-sig"))
            client = FakeSlackSocketClient(envelopes=list(envelopes))
            fake_web_client = FakeSlackWebClient(channel_id="DTEST")
            socket_config = SlackSocketConfig(
                app_token=socket_config.app_token or "xapp-test",
                dm_config=SlackDmConfig(
                    actor_id=args.actor,
                    dm_channel_id="DTEST",
                    bot_token=socket_config.dm_config.bot_token or "xoxb-test",
                    user_id=socket_config.dm_config.user_id,
                    bot_user_id=socket_config.dm_config.bot_user_id,
                    chat_id=socket_config.dm_config.chat_id,
                    instance_id=socket_config.dm_config.instance_id,
                    allowed_instance_id=socket_config.dm_config.allowed_instance_id,
                    watched_channel_ids=socket_config.dm_config.watched_channel_ids,
                    watch_requires_mention=socket_config.dm_config.watch_requires_mention,
                ),
            )
        try:
            adapter = SlackDmAdapter(
                socket_config.dm_config,
                fake_web_client,
                oldest=store.get_integration_state(f"slack.dm.{socket_config.dm_config.actor_id}.last_ts") or "",
            )
            result = asyncio.run(
                run_slack_socket_loop(
                    store=store,
                    orchestrator=TeamTaskOrchestrator(store, operating_agent=_build_operating_agent(args.agent)),
                    adapter=adapter,
                    socket_config=socket_config,
                    socket_client=client,
                    dashboard_output=args.dashboard_output,
                    send=args.send,
                    max_events=args.max_events,
                    max_seconds=args.max_seconds,
                    reconnect=False if args.transcript_input else not args.no_reconnect,
                    home_dashboard_url=args.home_dashboard_url,
                )
            )
        except SlackSocketError as exc:
            raise SystemExit(f"Slack Socket Mode not configured: {exc}") from exc
        except SlackAdapterError as exc:
            raise SystemExit(f"Slack adapter not configured: {exc}") from exc
        _print_json(
            {
                "connected": result.connected,
                "event_count": result.event_count,
                "message_count": result.message_count,
                "result_count": result.result_count,
                "outbound_count": result.outbound_count,
                "dashboard_output": str(result.dashboard_output),
                "stopped_reason": result.stopped_reason,
                "send": args.send,
                "fake_acks": client.acks if client is not None else [],
                "fake_sent": fake_web_client.sent if fake_web_client is not None else [],
                "fake_home_views": fake_web_client.home_views if fake_web_client is not None else [],
            }
        )
    elif args.command == "slack-fast-cycle":
        now = datetime.fromisoformat(args.now)
        client = None
        fake_sent = []
        if args.transcript_input:
            raw_messages = json.loads(args.transcript_input.read_text(encoding="utf-8-sig"))
            config = SlackDmConfig(actor_id=args.actor, dm_channel_id="DTEST")
            client = FakeSlackWebClient(messages=list(raw_messages), channel_id=config.dm_channel_id)
            adapter = SlackDmAdapter(
                config,
                client,
                oldest=store.get_integration_state(f"slack.dm.{config.actor_id}.last_ts") or "",
            )
        else:
            config = SlackDmConfig.from_env()
            try:
                adapter = SlackDmAdapter(
                    config,
                    oldest=store.get_integration_state(f"slack.dm.{config.actor_id}.last_ts") or "",
                )
            except SlackAdapterError as exc:
                raise SystemExit(f"Slack adapter not configured: {exc}") from exc
        try:
            result = run_slack_fast_cycle(
                store=store,
                orchestrator=TeamTaskOrchestrator(store, operating_agent=_build_operating_agent(args.agent)),
                adapter=adapter,
                now=now,
                dashboard_output=args.dashboard_output,
                send=args.send,
                home_dashboard_url=args.home_dashboard_url,
            )
        except SlackAdapterError as exc:
            raise SystemExit(f"Slack adapter not configured: {exc}") from exc
        if client is not None:
            fake_sent = list(client.sent)
        _print_json(
            {
                "message_count": len(result.messages),
                "result_count": len(result.results),
                "outbound_count": len(result.poll.outbound_messages) + result.pending_missing_info_followup_count,
                "pending_missing_info_request_count": result.pending_missing_info_request_count,
                "pending_missing_info_followup_count": result.pending_missing_info_followup_count,
                "dashboard_output": str(result.dashboard_output),
                "send": args.send,
                "fake_sent": fake_sent,
                "fake_home_views": client.home_views if client is not None else [],
            }
        )
    elif args.command == "dogfood-loop":
        _print_json(_run_dogfood_loop(args, store))
    elif args.command == "slack-task-cycle":
        now = datetime.fromisoformat(args.now)
        month = date.fromisoformat(f"{args.month}-01")
        client = None
        fake_sent = []
        if args.transcript_input:
            raw_messages = json.loads(args.transcript_input.read_text(encoding="utf-8-sig"))
            config = SlackDmConfig(actor_id=args.actor, dm_channel_id="DTEST")
            client = FakeSlackWebClient(messages=list(raw_messages), channel_id=config.dm_channel_id)
            adapter = SlackDmAdapter(
                config,
                client,
                oldest=store.get_integration_state(f"slack.dm.{config.actor_id}.last_ts") or "",
            )
        else:
            config = SlackDmConfig.from_env()
            try:
                adapter = SlackDmAdapter(
                    config,
                    oldest=store.get_integration_state(f"slack.dm.{config.actor_id}.last_ts") or "",
                )
            except SlackAdapterError as exc:
                raise SystemExit(f"Slack adapter not configured: {exc}") from exc
        try:
            result = run_slack_task_cycle(
                store=store,
                orchestrator=TeamTaskOrchestrator(store, operating_agent=_build_operating_agent(args.agent)),
                adapter=adapter,
                now=now,
                dashboard_output=args.dashboard_output,
                monthly_page_output=args.monthly_output,
                digest_output=args.digest_output,
                month=month,
                actor_id=args.actor,
                dashboard_url=args.dashboard_url,
                canvas_url=args.canvas_url,
                send=args.send,
            )
        except SlackAdapterError as exc:
            raise SystemExit(f"Slack adapter not configured: {exc}") from exc
        if client is not None:
            fake_sent = list(client.sent)
        _print_json(
            {
                "message_count": len(result.messages),
                "result_count": len(result.results),
                "reconciled_count": len(result.reconciled_proposals),
                "outbound_count": len(result.poll.outbound_messages),
                "digest_chars": len(result.digest_message.text),
                "dashboard_output": str(result.dashboard_output),
                "monthly_output": str(result.monthly_page_output),
                "digest_output": str(result.digest_output) if result.digest_output else "",
                "send": args.send,
                "fake_sent": fake_sent,
            }
        )
    elif args.command == "reminders":
        now = datetime.fromisoformat(args.now)
        messages = build_due_reminders(store, now=now, reserve=args.send)
        if args.send and messages:
            config = SlackDmConfig.from_env()
            try:
                adapter = SlackDmAdapter(config)
                for message in messages:
                    adapter.send_personal(message.recipient_id, message.text)
            except SlackAdapterError as exc:
                raise SystemExit(f"Slack adapter not configured: {exc}") from exc
        _print_json([asdict(item) for item in messages])
    elif args.command == "morning-briefing":
        now = datetime.fromisoformat(args.now)
        messages = build_morning_briefing(
            store,
            now=now,
            actor_id=args.actor,
            dashboard_url=args.dashboard_url,
            reserve=False if args.send else args.send,
        )
        if args.send and messages:
            _send_personal_messages(store, messages, sent_at=now)
        _print_json([asdict(item) for item in messages])
    elif args.command == "afternoon-briefing":
        now = datetime.fromisoformat(args.now)
        messages = build_afternoon_briefing(
            store,
            now=now,
            actor_id=args.actor,
            dashboard_url=args.dashboard_url,
            reserve=False if args.send else args.send,
        )
        if args.send and messages:
            _send_personal_messages(store, messages, sent_at=now)
        _print_json([asdict(item) for item in messages])
    elif args.command == "proactive-checks":
        now = datetime.fromisoformat(args.now)
        messages = build_proactive_checks(store, now=now, actor_id=args.actor, reserve=False if args.send else args.send)
        if args.send and messages:
            _send_personal_messages(store, messages, sent_at=now)
        _print_json([asdict(item) for item in messages])
    elif args.command == "end-of-day-review":
        now = datetime.fromisoformat(args.now)
        messages = build_end_of_day_review(store, now=now, actor_id=args.actor, reserve=False if args.send else args.send)
        if args.send and messages:
            _send_personal_messages(store, messages, sent_at=now)
        _print_json([asdict(item) for item in messages])
    elif args.command == "slack-e2e-clean":
        now = datetime.fromisoformat(args.now)
        config = SlackDmConfig.from_env()
        try:
            adapter = SlackDmAdapter(config)
            result = cleanup_live_slack_e2e(
                store=store,
                state_dir=args.state,
                adapter=adapter,
                dashboard_output=args.dashboard_output,
                now=now,
                reset_state=not args.keep_state,
                include_db_outbound=not args.skip_db_outbound,
                history_limit=args.history_limit,
            )
        except SlackAdapterError as exc:
            raise SystemExit(f"Slack adapter not configured: {exc}") from exc
        _print_json(result_to_dict(result))
    elif args.command == "slack-e2e-run":
        now = datetime.fromisoformat(args.now)
        run_id = args.run_id or now.strftime("%Y%m%dT%H%M%S")
        config = SlackDmConfig.from_env()
        try:
            adapter = SlackDmAdapter(config)
            result = run_live_slack_e2e(
                store=store,
                state_dir=args.state,
                orchestrator=TeamTaskOrchestrator(store, operating_agent=_build_operating_agent(args.agent)),
                adapter=adapter,
                dashboard_output=args.dashboard_output,
                now=now,
                run_id=run_id,
                send_inputs=args.send_inputs,
                send_replies=args.send_replies,
                send_summary=args.send_summary,
                checkpoint=not args.no_checkpoint,
            )
        except SlackAdapterError as exc:
            raise SystemExit(f"Slack adapter not configured: {exc}") from exc
        _print_json(result_to_dict(result))


def _store(state_dir: Path) -> TeamTaskStore:
    return TeamTaskStore(state_dir / "task_management.sqlite3", state_dir / "events.jsonl")


def _send_personal_messages(store: TeamTaskStore, messages, *, sent_at: datetime) -> None:
    config = SlackDmConfig.from_env()
    try:
        adapter = SlackDmAdapter(config)
        dispatch_slack_outbound(store, adapter, tuple(messages), sent_at=sent_at)
    except SlackAdapterError as exc:
        raise SystemExit(f"Slack adapter not configured: {exc}") from exc


def _socket_check_payload(check) -> dict[str, object]:
    return {
        "ok": check.ok,
        "app_token_set": check.app_token_set,
        "app_token_kind": check.app_token_kind,
        "required_app_scopes": list(check.required_app_scopes),
        "dm_ok": check.dm_ok,
        "watched_channel_ids": list(check.watched_channel_ids),
        "watch_requires_mention": check.watch_requires_mention,
        "errors": list(check.errors),
        "warnings": list(check.warnings),
        "setup_steps": list(check.setup_steps),
        "prints_token": False,
    }


def _run_dogfood_loop(args: argparse.Namespace, store: TeamTaskStore) -> dict[str, object]:
    if args.interval_seconds <= 0 and args.max_cycles == 0:
        raise SystemExit("dogfood-loop requires --max-cycles when --interval-seconds is 0 or negative")

    stop_file = args.stop_file or (args.state / "STOP")
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    cycles: list[dict[str, object]] = []
    fake_sent: list[tuple[str, str, str]] = []
    stopped_by_stop_file = False

    while True:
        if stop_file.exists():
            stopped_by_stop_file = True
            break
        now = datetime.now()
        client = None
        if args.transcript_input:
            raw_messages = json.loads(args.transcript_input.read_text(encoding="utf-8-sig"))
            config = SlackDmConfig(actor_id=args.actor, dm_channel_id="DTEST")
            client = FakeSlackWebClient(messages=list(raw_messages), channel_id=config.dm_channel_id)
            adapter = SlackDmAdapter(
                config,
                client,
                oldest=store.get_integration_state(f"slack.dm.{config.actor_id}.last_ts") or "",
            )
        else:
            config = SlackDmConfig.from_env()
            try:
                adapter = SlackDmAdapter(
                    config,
                    oldest=store.get_integration_state(f"slack.dm.{config.actor_id}.last_ts") or "",
                )
            except SlackAdapterError as exc:
                raise SystemExit(f"Slack adapter not configured: {exc}") from exc

        try:
            result = run_slack_fast_cycle(
                store=store,
                orchestrator=TeamTaskOrchestrator(store, operating_agent=_build_operating_agent(args.agent)),
                adapter=adapter,
                now=now,
                dashboard_output=args.dashboard_output,
                send=args.send,
            )
        except SlackAdapterError as exc:
            raise SystemExit(f"Slack adapter not configured: {exc}") from exc
        if client is not None:
            fake_sent.extend(client.sent)
        cycles.append(
            {
                "cycle": len(cycles) + 1,
                "message_count": len(result.messages),
                "result_count": len(result.results),
                "outbound_count": len(result.poll.outbound_messages),
                "dashboard_output": str(result.dashboard_output),
            }
        )
        if args.max_cycles and len(cycles) >= args.max_cycles:
            break
        if args.interval_seconds > 0:
            time.sleep(args.interval_seconds)

    store.append_event(
        "dogfood.loop.stopped",
        {
            "cycles": len(cycles),
            "stopped_by_stop_file": stopped_by_stop_file,
            "stop_file": str(stop_file),
            "send": args.send,
        },
        occurred_at=datetime.now(),
    )
    return {
        "mode": "private-user-only-slack-dm",
        "cycles": len(cycles),
        "cycle_summaries": cycles,
        "stopped_by_stop_file": stopped_by_stop_file,
        "stop_file": str(stop_file),
        "dashboard_output": str(args.dashboard_output),
        "send": args.send,
        "transcript_input": str(args.transcript_input) if args.transcript_input else "",
        "fake_sent": fake_sent,
        "safety": {
            "kakao_live": False,
            "public_deploy": False,
            "task_core_writes": False,
            "slack_scope": "personal_dm_only",
        },
    }


def _run_demo(args: argparse.Namespace) -> dict[str, object]:
    simulator = TeamTaskSimulator(args.state, operating_agent=_build_operating_agent(args.agent))
    actions = json.loads(args.fixture.read_text(encoding="utf-8-sig"))
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    action_summaries: list[dict[str, object]] = []
    outbound_messages: list[dict[str, object]] = []
    for index, action in enumerate(actions, start=1):
        action_type = str(action["type"])
        when = datetime.fromisoformat(str(action["at"]))
        if action_type == "dm":
            result = simulator.send_private(
                str(action.get("sender_id", args.actor)),
                str(action["text"]),
                message_id=str(action.get("message_id") or f"demo/dm/{index}"),
                received_at=when,
            )
        elif action_type == "family":
            result = simulator.send_family(
                str(action.get("sender_id", args.actor)),
                str(action["text"]),
                message_id=str(action.get("message_id") or f"demo/family/{index}"),
                received_at=when,
            )
        elif action_type in {"approve_latest", "reject_latest"}:
            request_id = str(action.get("request_id") or "")
            approver_id = str(action.get("approver_id", args.actor))
            if not request_id:
                request_id = _latest_pending_request_id(simulator.store, approver_id=approver_id) or ""
            if not request_id:
                result = OrchestrationResult(ignored_duplicate=True)
            elif action_type == "approve_latest":
                result = simulator.approve(request_id, approver_id, decided_at=when)
            else:
                result = simulator.reject(request_id, approver_id, decided_at=when)
        else:
            raise ValueError(f"unknown demo fixture action: {action_type}")

        outbound_messages.extend(asdict(message) for message in result.outbound_messages)
        action_summaries.append(
            {
                "index": index,
                "type": action_type,
                "proposal_count": len(result.proposals),
                "approval_request_count": len(result.approval_requests),
                "outbound_count": len(result.outbound_messages),
                "ignored_duplicate": result.ignored_duplicate,
            }
        )

    today = date.fromisoformat(args.today)
    month = date.fromisoformat(f"{args.month}-01")
    dashboard_output = output_dir / "dashboard.html"
    monthly_output = output_dir / "slack-monthly-task-page.md"
    preview_output = output_dir / "task-core-preview.json"
    outbox_output = output_dir / "simulated-dm-outbox.md"
    summary_output = output_dir / "summary.json"

    dashboard_model = build_web_task_page_model(simulator.store, today=today)
    dashboard_output.write_text(render_web_task_page_html(dashboard_model), encoding="utf-8")
    monthly_model = build_slack_monthly_task_page_model(simulator.store, actor_id=args.actor, month=month)
    monthly_output.write_text(render_slack_monthly_task_page_markdown(monthly_model), encoding="utf-8")
    preview_result = preview_approved_proposals(
        simulator.store,
        exported_at=datetime.combine(today, datetime.min.time()),
        task_core_root=args.task_core_root,
    )
    preview_output.write_text(
        json.dumps(
            {
                "payload": preview_result.payload,
                "preview": preview_result.preview,
                "applied_proposal_ids": list(preview_result.applied_proposal_ids),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    outbox_count = len(outbound_messages)
    if outbound_messages or not outbox_output.exists():
        outbox_output.write_text(_render_demo_outbox(outbound_messages), encoding="utf-8")
    else:
        outbox_count = _count_demo_outbox_messages(outbox_output.read_text(encoding="utf-8"))

    proposals = simulator.store.list_proposals()
    summary = {
        "state": str(args.state),
        "fixture": str(args.fixture),
        "outputs": {
            "dashboard": str(dashboard_output),
            "monthly_page": str(monthly_output),
            "task_core_preview": str(preview_output),
            "simulated_dm_outbox": str(outbox_output),
            "summary": str(summary_output),
        },
        "actions": action_summaries,
        "counts": {
            "proposals": len(proposals),
            "approved": len([item for item in proposals if item.status == "approved"]),
            "awaiting_approval": len([item for item in proposals if item.status == "awaiting_approval"]),
            "questions": len([item for item in proposals if item.kind == "question"]),
            "preview_items": len(preview_result.payload["items"]),
            "outbound_messages": outbox_count,
            "current_run_outbound_messages": len(outbound_messages),
            "events": len(simulator.store.read_events()),
        },
        "preview": {
            "ok": bool(preview_result.preview.get("ok")),
            "restores": preview_result.preview.get("restores"),
            "mutates_files": preview_result.payload.get("diagnostics", {}).get("mutates_files"),
        },
    }
    summary_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _latest_pending_request_id(store: TeamTaskStore, *, approver_id: str) -> str | None:
    requests = store.list_approval_requests(status="pending")
    if approver_id:
        scoped = [request for request in requests if request.approver_id == approver_id]
        if scoped:
            return scoped[-1].request_id
    if not requests:
        return None
    return requests[-1].request_id


def _render_demo_outbox(messages: list[dict[str, object]]) -> str:
    lines = ["# Simulated DM outbox", ""]
    if not messages:
        lines.append("_No personal DM replies were produced._")
    for index, message in enumerate(messages, start=1):
        lines.extend(
            [
                f"## {index}. {message.get('message_type', 'message')}",
                "",
                f"- surface: `{message.get('surface', '')}`",
                f"- recipient: `{message.get('recipient_id', '')}`",
                f"- proposal: `{message.get('proposal_id', '')}`",
                f"- approval: `{message.get('approval_request_id', '')}`",
                "",
                str(message.get("text", "")),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _count_demo_outbox_messages(text: str) -> int:
    return len([line for line in text.splitlines() if line.startswith("## ")])


LIVE_SEMANTIC_COMMANDS = frozenset(
    {
        "slack-poll",
        "slack-fast-cycle",
        "slack-socket-loop",
        "slack-task-cycle",
        "dogfood-loop",
        "slack-e2e-run",
    }
)


def _resolve_operating_agent_name(name: str, *, command: str, transcript_input: Path | None = None) -> str:
    if name != "auto":
        return name
    if command in LIVE_SEMANTIC_COMMANDS and transcript_input is None:
        return "codex"
    return "rule"


def _build_operating_agent(name: str) -> TeamTaskOperatingAgent:
    if name == "rule":
        return RuleBasedTeamTaskOperatingAgent()
    if name == "codex":
        return CodexCliOperatingAgent()
    if name == "claude":
        return ClaudeCodeCliOperatingAgent()
    if name == "openai":
        return OpenAIResponsesOperatingAgent()
    raise ValueError(f"unknown operating agent: {name}")


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _aliases(values: list[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"alias must use name=actor_id: {value}")
        name, actor_id = value.split("=", 1)
        aliases[name.strip()] = actor_id.strip()
    return aliases


if __name__ == "__main__":
    main()
