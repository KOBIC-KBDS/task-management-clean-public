"""TeamTask task management preview adapter."""

from .discussion_adapter import parse_manual_discussion
from .claude_code_operating_agent import ClaudeCodeCliOperatingAgent, ClaudeCodeCliOperatingAgentConfig
from .codex_operating_agent import CodexCliOperatingAgent, CodexCliOperatingAgentConfig
from .export_service import export_and_mark_approved_proposals_applied, preview_approved_proposals
from .frontend import build_web_task_page_model, render_kakao_text_card, render_web_task_page_html
from .kakao_export_adapter import parse_kakao_text_export
from .openai_operating_agent import OpenAIOperatingAgentConfig, OpenAIResponsesOperatingAgent
from .operating_agent import (
    OPERATING_DECISION_OUTPUT_SCHEMA,
    OPERATING_AGENT_SCHEMA,
    OperatingAgentDecision,
    ProposalDraft,
    ProposalPatch,
    RuleBasedTeamTaskOperatingAgent,
)
from .orchestrator import TeamTaskOrchestrator
from .simulator import TeamTaskSimulator
from .slack_adapter import SlackDmAdapter, SlackDmConfig, run_slack_dm_once
from .slack_cycle import SlackTaskCycleResult, run_slack_task_cycle
from .slack_digest import build_today_update_digest
from .task_reconciler import reconcile_message
from .task_core_bridge import (
    build_task_management_task_export,
    build_task_management_task_export_from_proposals,
    validate_with_task_core,
)

__all__ = [
    "build_task_management_task_export",
    "build_task_management_task_export_from_proposals",
    "build_web_task_page_model",
    "ClaudeCodeCliOperatingAgent",
    "ClaudeCodeCliOperatingAgentConfig",
    "CodexCliOperatingAgent",
    "CodexCliOperatingAgentConfig",
    "export_and_mark_approved_proposals_applied",
    "TeamTaskOrchestrator",
    "TeamTaskSimulator",
    "OPERATING_AGENT_SCHEMA",
    "OPERATING_DECISION_OUTPUT_SCHEMA",
    "OperatingAgentDecision",
    "OpenAIOperatingAgentConfig",
    "OpenAIResponsesOperatingAgent",
    "parse_manual_discussion",
    "parse_kakao_text_export",
    "preview_approved_proposals",
    "ProposalDraft",
    "ProposalPatch",
    "render_kakao_text_card",
    "render_web_task_page_html",
    "reconcile_message",
    "run_slack_dm_once",
    "run_slack_task_cycle",
    "RuleBasedTeamTaskOperatingAgent",
    "SlackTaskCycleResult",
    "SlackDmAdapter",
    "SlackDmConfig",
    "build_today_update_digest",
    "validate_with_task_core",
]
