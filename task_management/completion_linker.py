from __future__ import annotations

from .relations import (
    EXTERNAL_PARTICIPANTS_KEY,
    LAST_SEMANTIC_PATCH_EVIDENCE_KEY,
    PARTICIPANTS_KEY,
    PARTICIPANT_LABEL_KEY,
    PROGRESS_NOTE_KEY,
)

import re
from typing import Sequence

from .domain import Proposal
from .operating_agent import ProposalPatch
from .sort_keys import time_sort_minutes


_RELATED_COMPLETION_STOPWORDS = frozenset(
    {
        "일정",
        "결정",
        "진행",
        "예정",
        "확정",
        "완료",
        "관련",
        "위에서",
        "말한",
        "해당",
        "시각",
        "회의",
        "미팅",
        "후속",
        "자료",
        "보고",
        "정리",
        "the",
        "and",
        "for",
        "with",
    }
)


def _same_scheduled_commitment_slot(candidate: Proposal, completed: Proposal) -> bool:
    if candidate.kind != "event" or candidate.status not in {"approved", "applied", "awaiting_approval"}:
        return False
    if candidate.scheduled_date is None or candidate.scheduled_date != completed.scheduled_date:
        return False
    if candidate.time_window and completed.time_window:
        return time_sort_minutes(candidate.time_window) == time_sort_minutes(completed.time_window)
    return True


def _has_related_completion_signal(candidate: Proposal, completed: Proposal, patch: ProposalPatch) -> bool:
    if _explicitly_linked(candidate, completed):
        return True
    candidate_tokens = _completion_topic_tokens(
        candidate.title,
        candidate.raw_text,
        *_metadata_topic_values(candidate),
    )
    completed_tokens = _completion_topic_tokens(
        completed.title,
        completed.raw_text,
        patch.body,
        patch.evidence_text,
        *_metadata_topic_values(completed),
    )
    if len(candidate_tokens & completed_tokens) >= 2:
        return True
    if candidate.metadata.get("decision_pending") == "true" or candidate.metadata.get("date_resolution_policy"):
        return bool(candidate_tokens & completed_tokens) and _shares_participant_hint(candidate, completed)
    return False


def _explicitly_linked(candidate: Proposal, completed: Proposal) -> bool:
    return any(
        value == completed.proposal_id
        for value in (
            candidate.metadata.get("parent_proposal_id", ""),
            candidate.metadata.get("linked_completion_source_proposal_id", ""),
        )
    ) or any(
        value == candidate.proposal_id
        for value in (
            completed.metadata.get("parent_proposal_id", ""),
            completed.metadata.get("linked_completion_source_proposal_id", ""),
        )
    )


def _metadata_topic_values(proposal: Proposal) -> tuple[str, ...]:
    keys = (
        "materials",
        EXTERNAL_PARTICIPANTS_KEY,
        PARTICIPANT_LABEL_KEY,
        "external_owner",
        PROGRESS_NOTE_KEY,
        "previous_title",
        LAST_SEMANTIC_PATCH_EVIDENCE_KEY,
    )
    return tuple(proposal.metadata.get(key, "") for key in keys if proposal.metadata.get(key))


def _completion_topic_tokens(*texts: str) -> set[str]:
    tokens: set[str] = set()
    for text in texts:
        for token in re.findall(r"[0-9A-Za-z가-힣]+", text.lower()):
            if token.isdigit() or len(token) < 2 or token in _RELATED_COMPLETION_STOPWORDS:
                continue
            tokens.add(token)
    return tokens


def _shares_participant_hint(candidate: Proposal, completed: Proposal) -> bool:
    candidate_people = _completion_topic_tokens(
        candidate.metadata.get(PARTICIPANTS_KEY, ""),
        candidate.metadata.get(EXTERNAL_PARTICIPANTS_KEY, ""),
        candidate.metadata.get(PARTICIPANT_LABEL_KEY, ""),
    )
    completed_people = _completion_topic_tokens(
        completed.metadata.get(PARTICIPANTS_KEY, ""),
        completed.metadata.get(EXTERNAL_PARTICIPANTS_KEY, ""),
        completed.metadata.get(PARTICIPANT_LABEL_KEY, ""),
    )
    return bool(candidate_people & completed_people)


def related_commitments(
    candidates: Sequence[Proposal],
    completed: Proposal,
    patch: ProposalPatch,
) -> tuple[Proposal, ...]:
    """Return the candidate proposals that share the completed event's scheduled
    slot and carry a related-completion signal.

    Pure matching logic only: callers own metadata stamping, event emission, and
    store writes. ``completed`` must already be the completed event; candidates
    that are the completed proposal itself are skipped.
    """

    if completed.kind != "event" or completed.scheduled_date is None:
        return ()
    matches: list[Proposal] = []
    for candidate in candidates:
        if candidate.proposal_id == completed.proposal_id:
            continue
        if not _same_scheduled_commitment_slot(candidate, completed):
            continue
        if not _has_related_completion_signal(candidate, completed, patch):
            continue
        matches.append(candidate)
    return tuple(matches)
