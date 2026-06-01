from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Mapping
from urllib import request as urlrequest


class SlackCanvasError(RuntimeError):
    """Raised for Slack Canvas API configuration or response failures."""


@dataclass(frozen=True)
class SlackCanvasConfig:
    canvas_id: str = ""
    token: str = ""

    @classmethod
    def from_env(cls) -> "SlackCanvasConfig":
        return cls(
            canvas_id=os.environ.get("SLACK_CANVAS_ID", ""),
            token=os.environ.get("SLACK_CANVAS_TOKEN", "") or os.environ.get("SLACK_BOT_TOKEN", ""),
        )


def build_canvas_replace_payload(markdown: str) -> dict[str, Any]:
    """Build a whole-canvas replacement payload for Slack `canvases.edit`."""

    return {
        "changes": [
            {
                "operation": "replace",
                "document_content": {
                    "type": "markdown",
                    "markdown": markdown,
                },
            }
        ]
    }


class SlackCanvasHttpClient:
    """Tiny stdlib Slack Canvas Web API client kept behind explicit CLI opt-in."""

    def __init__(self, token: str) -> None:
        if not token:
            raise SlackCanvasError("SLACK_CANVAS_TOKEN or SLACK_BOT_TOKEN is required")
        self.token = token

    def replace_canvas(self, canvas_id: str, markdown: str) -> dict[str, Any]:
        if not canvas_id:
            raise SlackCanvasError("SLACK_CANVAS_ID or --canvas-id is required")
        payload = {"canvas_id": canvas_id, **build_canvas_replace_payload(markdown)}
        return self._post_json("https://slack.com/api/canvases.edit", payload)

    def _post_json(self, url: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with urlrequest.urlopen(req, timeout=20) as response:  # nosec - explicit live CLI path only
            parsed = json.loads(response.read().decode("utf-8"))
        if not parsed.get("ok"):
            raise SlackCanvasError(str(parsed.get("error") or parsed))
        return parsed
