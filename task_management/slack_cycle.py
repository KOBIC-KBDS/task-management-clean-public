from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .domain import OrchestrationResult, OutboundMessage, Proposal
from .frontend import build_web_task_page_model, render_web_task_page_html
from .orchestrator import TeamTaskOrchestrator
from .slack_adapter import SlackDmAdapter, SlackPollResult, dispatch_slack_outbound, run_slack_dm_once
from .slack_digest import build_today_update_digest
from .slack_page import build_slack_monthly_task_page_model, render_slack_monthly_task_page_markdown
from .store import TeamTaskStore
from .task_reconciler import reconcile_message


@dataclass(frozen=True)
class SlackTaskCycleResult:
    poll: SlackPollResult
    reconciled_proposals: tuple[Proposal, ...]
    digest_message: OutboundMessage
    dashboard_output: Path
    monthly_page_output: Path
    digest_output: Path | None

    @property
    def messages(self):
        return self.poll.messages

    @property
    def results(self) -> tuple[OrchestrationResult, ...]:
        return self.poll.results


def run_slack_task_cycle(
    *,
    store: TeamTaskStore,
    orchestrator: TeamTaskOrchestrator,
    adapter: SlackDmAdapter,
    now: datetime,
    dashboard_output: Path,
    monthly_page_output: Path,
    month: date,
    actor_id: str = "me",
    dashboard_url: str = "",
    canvas_url: str = "",
    digest_output: Path | None = None,
    send: bool = False,
) -> SlackTaskCycleResult:
    """Run one personal Slack DM intake/update cycle.

    The cycle is still simulator/adapter-first: it polls a configured Slack DM
    source, lets the operating agent create proposals, runs narrow deterministic
    reconciliation for state-linked follow-ups, renders local web/Canvas files,
    then optionally sends a compact personal DM digest.
    """

    poll = run_slack_dm_once(
        store=store,
        orchestrator=orchestrator,
        adapter=adapter,
        send=False,
        now=now,
    )
    reconciled: list[Proposal] = []
    for message, result in zip(poll.messages, poll.results):
        reconciled.extend(reconcile_message(store, message, result, reconciled_at=now))

    dashboard_output.parent.mkdir(parents=True, exist_ok=True)
    dashboard_model = build_web_task_page_model(store, today=now.date())
    dashboard_output.write_text(render_web_task_page_html(dashboard_model), encoding="utf-8")

    monthly_page_output.parent.mkdir(parents=True, exist_ok=True)
    monthly_model = build_slack_monthly_task_page_model(store, actor_id=actor_id, month=month)
    monthly_page_output.write_text(render_slack_monthly_task_page_markdown(monthly_model), encoding="utf-8")

    digest = build_today_update_digest(
        store,
        now=now,
        actor_id=actor_id,
        dashboard_url=dashboard_url,
        canvas_url=canvas_url,
    )
    if digest_output is not None:
        digest_output.parent.mkdir(parents=True, exist_ok=True)
        digest_output.write_text(digest + "\n", encoding="utf-8")
    digest_message = OutboundMessage(
        surface="personal_chat",
        recipient_id=actor_id,
        message_type="task_cycle_digest",
        text=digest,
        card={
            "message_count": str(len(poll.messages)),
            "result_count": str(len(poll.results)),
            "reconciled_count": str(len(reconciled)),
            "dashboard_output": str(dashboard_output),
            "monthly_page_output": str(monthly_page_output),
            "canvas_url": canvas_url,
        },
    )
    store.append_event(
        "slack.task_cycle.digest.created",
        {
            "message_count": len(poll.messages),
            "result_count": len(poll.results),
            "reconciled_count": len(reconciled),
            "dashboard_output": str(dashboard_output),
            "monthly_page_output": str(monthly_page_output),
        },
        occurred_at=now,
    )
    if send:
        dispatch_slack_outbound(store, adapter, (digest_message,), sent_at=now)
    return SlackTaskCycleResult(
        poll=poll,
        reconciled_proposals=tuple(reconciled),
        digest_message=digest_message,
        dashboard_output=dashboard_output,
        monthly_page_output=monthly_page_output,
        digest_output=digest_output,
    )
