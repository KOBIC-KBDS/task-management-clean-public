"""Compatibility module for Slack-oriented task_management intake.

The live/testable Slack DM adapter lives in :mod:`task_management.slack_adapter`.
This module remains as the local export/transcript boundary name used by older
notes and tests.
"""

from .slack_adapter import FakeSlackWebClient, SlackDmAdapter, SlackDmConfig, run_slack_dm_once

__all__ = ["FakeSlackWebClient", "SlackDmAdapter", "SlackDmConfig", "run_slack_dm_once"]
