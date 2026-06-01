from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .domain import OrchestrationResult
from .frontend import build_web_task_page_model, render_web_task_page_html
from .orchestrator import TeamTaskOrchestrator
from .pending_info import build_pending_missing_info_followups
from .slack_adapter import SlackDmAdapter, SlackPollResult, drain_slack_outbound_queue, run_slack_dm_once
from .slack_home import publish_slack_home_tab
from .store import TeamTaskStore


@dataclass(frozen=True)
class SlackFastCycleResult:
    """Result of the lightweight runtime loop.

    The fast cycle is the production-shaped path for normal operation:
    read new DM messages, let the semantic operating agent produce strict
    decisions, let the deterministic core validate/commit, then refresh the web
    task page.  It intentionally avoids development-time extras such as digest
    generation, Slack Canvas snapshots, and secondary reconciliation passes.
    """

    poll: SlackPollResult
    dashboard_output: Path
    pending_missing_info_request_count: int = 0
    pending_missing_info_followup_count: int = 0

    @property
    def messages(self):
        return self.poll.messages

    @property
    def results(self) -> tuple[OrchestrationResult, ...]:
        return self.poll.results


def run_slack_fast_cycle(
    *,
    store: TeamTaskStore,
    orchestrator: TeamTaskOrchestrator,
    adapter: SlackDmAdapter,
    now: datetime,
    dashboard_output: Path,
    send: bool = False,
    home_dashboard_url: str = "",
) -> SlackFastCycleResult:
    """Run the simple Slack DM -> semantic decision -> state -> web loop.

    Use this path for day-to-day task_management operation when the goal is quick
    intake and a direct clarification/confirmation reply.  `slack-task-cycle`
    remains available for heavier snapshot/digest workflows.
    """

    poll = run_slack_dm_once(
        store=store,
        orchestrator=orchestrator,
        adapter=adapter,
        send=send,
        now=now,
    )
    followup_request_count = 0
    followup_sent_count = 0
    if send:
        touched_proposal_ids = {
            proposal.proposal_id
            for result in poll.results
            for proposal in result.proposals
        }
        followup_requests = build_pending_missing_info_followups(
            store,
            now=now,
            actor_id=adapter.config.actor_id,
            exclude_proposal_ids=touched_proposal_ids,
        )
        followup_request_count = len(followup_requests)
        followup_sent_count = drain_slack_outbound_queue(store, adapter, sent_at=now, raise_on_error=True)

    dashboard_output.parent.mkdir(parents=True, exist_ok=True)
    dashboard_model = build_web_task_page_model(store, today=now.date())
    dashboard_output.write_text(render_web_task_page_html(dashboard_model), encoding="utf-8")
    home_published = False
    if send and home_dashboard_url:
        home_published = publish_slack_home_tab(
            store,
            adapter,
            now=now,
            dashboard_url=home_dashboard_url,
        )

    store.append_event(
        "slack.fast_cycle.completed",
        {
            "message_count": len(poll.messages),
            "result_count": len(poll.results),
            "outbound_count": len(poll.outbound_messages) + followup_sent_count,
            "pending_missing_info_request_count": followup_request_count,
            "pending_missing_info_followup_count": followup_sent_count,
            "dashboard_output": str(dashboard_output),
            "home_published": home_published,
            "send": send,
        },
        occurred_at=now,
    )
    return SlackFastCycleResult(
        poll=poll,
        dashboard_output=dashboard_output,
        pending_missing_info_request_count=followup_request_count,
        pending_missing_info_followup_count=followup_sent_count,
    )
