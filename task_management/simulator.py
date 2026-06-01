from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .domain import IncomingMessage, OrchestrationResult
from .operating_agent import TeamTaskOperatingAgent
from .orchestrator import TeamTaskOrchestrator
from .store import TeamTaskStore


class TeamTaskSimulator:
    """Fixture-friendly facade for the one-bot, many-agent MVP."""

    def __init__(self, root: str | Path, *, operating_agent: TeamTaskOperatingAgent | None = None) -> None:
        root_path = Path(root)
        self.store = TeamTaskStore(root_path / "task_management.sqlite3", root_path / "events.jsonl")
        self.orchestrator = TeamTaskOrchestrator(self.store, operating_agent=operating_agent)
        self._sequence = 0

    def send_private(
        self,
        sender_id: str,
        text: str,
        *,
        message_id: str | None = None,
        received_at: datetime,
    ) -> OrchestrationResult:
        return self.orchestrator.handle_message(
            IncomingMessage(
                message_id=message_id or self._next_message_id("private", sender_id),
                sender_id=sender_id,
                chat_id=f"dm/{sender_id}",
                visibility="private",
                text=text,
                received_at=received_at,
            )
        )

    def send_team(
        self,
        sender_id: str,
        text: str,
        *,
        message_id: str | None = None,
        received_at: datetime,
    ) -> OrchestrationResult:
        return self.orchestrator.handle_message(
            IncomingMessage(
                message_id=message_id or self._next_message_id("team", sender_id),
                sender_id=sender_id,
                chat_id="team",
                visibility="team",
                text=text,
                received_at=received_at,
            )
        )

    def approve(self, request_id: str, approver_id: str, *, decided_at: datetime) -> OrchestrationResult:
        return self.orchestrator.handle_approval(
            request_id=request_id,
            approver_id=approver_id,
            accepted=True,
            decided_at=decided_at,
        )

    def reject(self, request_id: str, approver_id: str, *, decided_at: datetime) -> OrchestrationResult:
        return self.orchestrator.handle_approval(
            request_id=request_id,
            approver_id=approver_id,
            accepted=False,
            decided_at=decided_at,
        )

    def run_fixture(self, actions: Iterable[dict[str, Any]]) -> tuple[OrchestrationResult, ...]:
        results: list[OrchestrationResult] = []
        for action in actions:
            action_type = action["type"]
            when = datetime.fromisoformat(action["at"])
            if action_type == "private":
                results.append(
                    self.send_private(
                        action["sender_id"],
                        action["text"],
                        message_id=action.get("message_id"),
                        received_at=when,
                    )
                )
            elif action_type == "team":
                results.append(
                    self.send_team(
                        action["sender_id"],
                        action["text"],
                        message_id=action.get("message_id"),
                        received_at=when,
                    )
                )
            elif action_type == "approve":
                results.append(self.approve(action["request_id"], action["approver_id"], decided_at=when))
            elif action_type == "reject":
                results.append(self.reject(action["request_id"], action["approver_id"], decided_at=when))
            else:
                raise ValueError(f"unknown simulator action: {action_type}")
        return tuple(results)

    def _next_message_id(self, scope: str, sender_id: str) -> str:
        self._sequence += 1
        return f"sim/{scope}/{sender_id}/{self._sequence}"
