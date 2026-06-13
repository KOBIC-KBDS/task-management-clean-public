from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from typing import Any, Mapping, Protocol, Sequence
from urllib import request as urlrequest

from .domain import ApprovalRequest, IncomingMessage, Proposal
from .operating_agent import (
    OPERATING_DECISION_OUTPUT_SCHEMA,
    TeamTaskOperatingAgent,
    OperatingAgentDecision,
    RuleBasedTeamTaskOperatingAgent,
    decision_from_payload,
)
from .operating_agent_prompt import build_operating_agent_system_instructions
from .semantic_context import build_operating_agent_context


class OpenAIOperatingAgentError(RuntimeError):
    """Raised when the optional OpenAI operating-agent adapter cannot produce a valid decision."""


@dataclass(frozen=True)
class OpenAIOperatingAgentConfig:
    api_key: str = ""
    model: str = "gpt-5.5"
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 30.0
    fallback_on_error: bool = True
    strict_schema: bool = True

    @classmethod
    def from_env(cls) -> "OpenAIOperatingAgentConfig":
        return cls(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            model=os.environ.get("TASK_MANAGEMENT_OPENAI_MODEL", "gpt-5.5"),
            base_url=os.environ.get("TASK_MANAGEMENT_OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout_seconds=float(os.environ.get("TASK_MANAGEMENT_OPENAI_TIMEOUT_SECONDS", "30")),
            fallback_on_error=os.environ.get("TASK_MANAGEMENT_OPENAI_FALLBACK", "1").lower() not in {"0", "false", "no", "off"},
            strict_schema=os.environ.get("TASK_MANAGEMENT_OPENAI_STRICT_SCHEMA", "1").lower() not in {"0", "false", "no", "off"},
        )


class OpenAIResponsesClient(Protocol):
    def create_response(self, payload: Mapping[str, Any], *, timeout_seconds: float) -> Mapping[str, Any]:
        """Create a Responses API result and return decoded JSON."""


class StdlibOpenAIResponsesClient:
    """Small stdlib Responses API client to avoid adding a hard OpenAI SDK dependency."""

    def __init__(self, *, api_key: str, base_url: str = "https://api.openai.com/v1") -> None:
        if not api_key:
            raise OpenAIOperatingAgentError("OPENAI_API_KEY is required for the OpenAI operating agent")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def create_response(self, payload: Mapping[str, Any], *, timeout_seconds: float) -> Mapping[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            f"{self.base_url}/responses",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with urlrequest.urlopen(req, timeout=timeout_seconds) as response:  # nosec - explicit opt-in adapter
            return json.loads(response.read().decode("utf-8"))


class OpenAIResponsesOperatingAgent:
    """Optional LLM operating agent that can only propose strict decisions.

    The deterministic fallback remains available because Slack/home-server polling should not
    stop merely because a model call, network, or schema response fails.
    """

    def __init__(
        self,
        config: OpenAIOperatingAgentConfig | None = None,
        *,
        client: OpenAIResponsesClient | None = None,
        fallback_agent: TeamTaskOperatingAgent | None = None,
    ) -> None:
        self.config = config or OpenAIOperatingAgentConfig.from_env()
        self.client = client
        self.fallback_agent = fallback_agent or RuleBasedTeamTaskOperatingAgent()

    def decide(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
    ) -> OperatingAgentDecision:
        fallback_decision = self.fallback_agent.decide(
            message,
            pending_approval_requests=pending_approval_requests,
            pending_proposals=pending_proposals,
        )
        try:
            client = self._client()
            request_payload = self._request_payload(
                message,
                pending_approval_requests=pending_approval_requests,
                pending_proposals=pending_proposals,
                fallback_decision=fallback_decision,
            )
            response_payload = client.create_response(request_payload, timeout_seconds=self.config.timeout_seconds)
            decision_payload = json.loads(_extract_output_text(response_payload))
            decision = decision_from_payload(decision_payload)
            return replace(
                decision,
                source="openai_responses",
                rationale=_with_model_note(decision.rationale, self.config.model),
            )
        except Exception as exc:  # noqa: BLE001 - boundary intentionally catches all model/network/schema failures
            if not self.config.fallback_on_error:
                raise OpenAIOperatingAgentError(str(exc)) from exc
            return replace(
                fallback_decision,
                source="rule_based_fallback",
                rationale=f"OpenAI operating agent unavailable or invalid; used rule fallback. reason={exc}",
            )

    def _client(self) -> OpenAIResponsesClient:
        if self.client is not None:
            return self.client
        return StdlibOpenAIResponsesClient(api_key=self.config.api_key, base_url=self.config.base_url)

    def _request_payload(
        self,
        message: IncomingMessage,
        *,
        pending_approval_requests: Sequence[ApprovalRequest],
        pending_proposals: Sequence[Proposal],
        fallback_decision: OperatingAgentDecision,
    ) -> dict[str, Any]:
        context = build_operating_agent_context(
            message,
            pending_approval_requests=pending_approval_requests,
            pending_proposals=pending_proposals,
            fallback_decision=fallback_decision,
        )
        return {
            "model": self.config.model,
            "instructions": _SYSTEM_INSTRUCTIONS,
            "input": json.dumps(context, ensure_ascii=False, sort_keys=True),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "task_management_operating_decision",
                    "schema": OPERATING_DECISION_OUTPUT_SCHEMA,
                    "strict": self.config.strict_schema,
                }
            },
        }


_SYSTEM_INSTRUCTIONS = (
    build_operating_agent_system_instructions(
        backend_runtime="the OpenAI Responses API",
        source_key_prefix="agent",
    )
    + "Return your single JSON object so it validates against the strict json_schema response format "
    + "(task_management_operating_decision) supplied with this request; emit only the schema fields and no extra keys.\n"
)


def _extract_output_text(response_payload: Mapping[str, Any]) -> str:
    output_text = response_payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    for item in response_payload.get("output", []) or []:
        if not isinstance(item, Mapping):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, Mapping):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return text
    raise OpenAIOperatingAgentError("OpenAI response did not include output text")


def _with_model_note(rationale: str, model: str) -> str:
    note = f"model={model}"
    return f"{rationale} ({note})" if rationale else note
