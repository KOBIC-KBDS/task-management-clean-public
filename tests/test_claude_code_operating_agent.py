from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from task_management.claude_code_operating_agent import (
    ClaudeCodeCliOperatingAgent,
    ClaudeCodeCliOperatingAgentConfig,
    ClaudeCodeOperatingAgentError,
    _claude_command,
    _subprocess_env,
)
from task_management.domain import IncomingMessage
from task_management.operating_agent import OPERATING_DECISION_OUTPUT_SCHEMA
from task_management.orchestrator import TeamTaskOrchestrator
from task_management.store import TeamTaskStore


NOW = datetime(2026, 5, 5, 10, 0, 0)
BABYFAIR_MESSAGE = "\uc6cc\ud06c\uc20d \uc774\ubc88\uc8fc \ubaa9~\uc77c \uc911 \ud558\ub8e8 \uac00\uc57c\ud568"
CLAUDE_TITLE = "Claude \uc6cc\ud06c\uc20d \ucc38\uc11d"


class RecordingClaudeRunner:
    def __init__(self, *, fail: bool = False, result_only: bool = False) -> None:
        self.fail = fail
        self.result_only = result_only
        self.prompts: list[str] = []
        self.schemas: list[Mapping[str, Any]] = []
        self.configs: list[ClaudeCodeCliOperatingAgentConfig] = []

    def run_decision(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any],
        config: ClaudeCodeCliOperatingAgentConfig,
    ) -> Mapping[str, Any]:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        self.configs.append(config)
        if self.fail:
            raise RuntimeError("claude unavailable")
        start = prompt.index("<task_management_context>") + len("<task_management_context>")
        end = prompt.index("</task_management_context>")
        context = json.loads(prompt[start:end])
        decision = context["deterministic_baseline"]
        decision["source"] = "model"
        decision["rationale"] = "Claude normalized task_management wording."
        decision["proposal_drafts"][0]["title"] = CLAUDE_TITLE
        if self.result_only:
            return {"is_error": False, "result": "```json\n" + json.dumps(decision, ensure_ascii=False) + "\n```"}
        return {"is_error": False, "structured_output": decision}


def _message() -> IncomingMessage:
    return IncomingMessage(
        message_id="dm/me/claude",
        sender_id="me",
        chat_id="dm/me",
        visibility="private",
        text=BABYFAIR_MESSAGE,
        received_at=NOW,
    )


def test_claude_code_cli_operating_agent_uses_login_session_contract_without_api_key() -> None:
    runner = RecordingClaudeRunner()
    agent = ClaudeCodeCliOperatingAgent(
        ClaudeCodeCliOperatingAgentConfig(model="sonnet"),
        runner=runner,
    )

    decision = agent.decide(_message(), pending_approval_requests=(), pending_proposals=())

    assert decision.source == "claude_code_cli"
    assert decision.proposal_drafts[0].title == CLAUDE_TITLE
    assert "claude_code_login_session" in decision.rationale
    assert "model=sonnet" in decision.rationale
    assert runner.schemas[0]["additionalProperties"] is False
    assert runner.configs[0].model == "sonnet"
    assert "Never request, print, or depend on an API key." in runner.prompts[0]
    assert '"auth_policy": "Use the local Claude Code login session only; do not ask for or output API keys."' in runner.prompts[0]
    assert "do not invent a new item_type" in runner.prompts[0]
    assert "type_policy_needed=true" in runner.prompts[0]
    assert "depends_on_proposal_ids" in runner.prompts[0]
    assert "step_index" in runner.prompts[0]
    assert "workflow_id" in runner.prompts[0]


def test_claude_code_cli_operating_agent_accepts_json_result_fallback() -> None:
    agent = ClaudeCodeCliOperatingAgent(
        ClaudeCodeCliOperatingAgentConfig(model="sonnet"),
        runner=RecordingClaudeRunner(result_only=True),
    )

    decision = agent.decide(_message(), pending_approval_requests=(), pending_proposals=())

    assert decision.source == "claude_code_cli"
    assert decision.proposal_drafts[0].title == CLAUDE_TITLE


def test_orchestrator_commits_claude_agent_draft_through_core_policy(tmp_path: Path) -> None:
    store = TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")
    agent = ClaudeCodeCliOperatingAgent(
        ClaudeCodeCliOperatingAgentConfig(model="sonnet"),
        runner=RecordingClaudeRunner(),
    )

    result = TeamTaskOrchestrator(store, operating_agent=agent).handle_message(_message())

    proposal = result.proposals[0]
    assert proposal.title == CLAUDE_TITLE
    assert proposal.kind == "question"
    assert proposal.status == "awaiting_approval"
    assert proposal.missing_slots == ("exact_date", "participants")
    decision_event = next(event for event in store.read_events() if event["type"] == "agent.decision.created")
    assert decision_event["payload"]["decision"]["source"] == "claude_code_cli"


def test_claude_code_cli_operating_agent_falls_back_to_rule_decision_on_error() -> None:
    agent = ClaudeCodeCliOperatingAgent(
        ClaudeCodeCliOperatingAgentConfig(fallback_on_error=True),
        runner=RecordingClaudeRunner(fail=True),
    )

    decision = agent.decide(_message(), pending_approval_requests=(), pending_proposals=())

    assert decision.source == "claude_code_cli_fallback"
    assert decision.action == "create_proposals"
    assert "claude unavailable" in decision.rationale


def test_orchestrator_logs_claude_fallback_as_runtime_observability(tmp_path: Path) -> None:
    store = TeamTaskStore(tmp_path / "task_management.sqlite3", tmp_path / "events.jsonl")
    agent = ClaudeCodeCliOperatingAgent(
        ClaudeCodeCliOperatingAgentConfig(fallback_on_error=True),
        runner=RecordingClaudeRunner(fail=True),
    )

    TeamTaskOrchestrator(store, operating_agent=agent).handle_message(_message())

    fallback_event = next(event for event in store.read_events() if event["type"] == "agent.fallback.used")
    assert fallback_event["payload"]["source"] == "claude_code_cli_fallback"
    assert fallback_event["payload"]["message_id"] == "dm/me/claude"


def test_claude_code_cli_operating_agent_can_fail_closed_when_fallback_is_disabled() -> None:
    agent = ClaudeCodeCliOperatingAgent(
        ClaudeCodeCliOperatingAgentConfig(fallback_on_error=False),
        runner=RecordingClaudeRunner(fail=True),
    )

    with pytest.raises(ClaudeCodeOperatingAgentError, match="claude unavailable"):
        agent.decide(_message(), pending_approval_requests=(), pending_proposals=())


def test_claude_config_defaults_to_fail_closed_and_strips_api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "TASK_MANAGEMENT_CLAUDE_FALLBACK",
        "TASK_MANAGEMENT_CLAUDE_STRIP_API_KEY_ENV",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("CLAUDE_API_KEY", "claude-secret")
    monkeypatch.setenv("CLAUDE_CODE_API_KEY", "claude-code-secret")

    config = ClaudeCodeCliOperatingAgentConfig.from_env()
    env = _subprocess_env(config)

    assert config.fallback_on_error is False
    assert config.strip_api_key_env is True
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_API_KEY" not in env
    assert "CLAUDE_CODE_API_KEY" not in env


def test_claude_print_mode_command_is_schema_bound_and_non_persistent() -> None:
    command = _claude_command(
        ClaudeCodeCliOperatingAgentConfig(
            model="sonnet",
            permission_mode="plan",
            max_turns=3,
            tools="",
            no_session_persistence=True,
        ),
        schema=OPERATING_DECISION_OUTPUT_SCHEMA,
    )

    assert "-p" in command
    assert command[command.index("--model") + 1] == "sonnet"
    assert "--no-session-persistence" in command
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--max-turns") + 1] == "3"
    assert command[command.index("--output-format") + 1] == "json"
    schema_text = command[command.index("--json-schema") + 1]
    assert json.loads(schema_text)["additionalProperties"] is False
