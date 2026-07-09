from __future__ import annotations

from dataclasses import dataclass
import re

from .domain import ChatCommand


COMMAND_RE = re.compile(
    r"^\s*(?P<verb>수락|승인|accept|ok|거절|reject|변경|change|완료|done|담당|assign)"
    r"\s+(?P<target>\S+)(?:\s+(?P<body>.+))?\s*$",
    re.IGNORECASE,
)
INSTANCE_PROBE_RE = re.compile(
    r"^\s*(?:instance-only|instance-probe|인스턴스전용응답)\s+"
    r"(?P<target_instance_id>\S+)\s+(?P<nonce>\S+)(?:\s+(?P<body>.+))?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class InstanceProbeCommand:
    target_instance_id: str
    nonce: str
    body: str = ""


def parse_chat_command(text: str) -> ChatCommand | None:
    match = COMMAND_RE.match(_normalize_command_text(text))
    if not match:
        return None
    verb = match.group("verb").lower()
    action = {
        "수락": "accept",
        "승인": "accept",
        "accept": "accept",
        "ok": "accept",
        "거절": "reject",
        "reject": "reject",
        "변경": "change",
        "change": "change",
        "완료": "complete",
        "done": "complete",
        "담당": "assign",
        "assign": "assign",
    }[verb]
    return ChatCommand(
        action=action,
        target_id=match.group("target"),
        body=(match.group("body") or "").strip(),
    )


def _normalize_command_text(text: str) -> str:
    normalized = text.strip()
    # Slack rich-text code/bold formatting can arrive as literal wrappers in
    # message.text.  The command contract is semantic, so strip only wrappers
    # that cover the whole command and leave the command body intact.
    changed = True
    while changed and len(normalized) >= 2:
        changed = False
        for marker in ("`", "*", "_", "~"):
            if normalized.startswith(marker) and normalized.endswith(marker):
                normalized = normalized[1:-1].strip()
                changed = True
    return normalized


def parse_instance_probe(text: str) -> InstanceProbeCommand | None:
    match = INSTANCE_PROBE_RE.match(text)
    if not match:
        return None
    return InstanceProbeCommand(
        target_instance_id=match.group("target_instance_id"),
        nonce=match.group("nonce"),
        body=(match.group("body") or "").strip(),
    )
