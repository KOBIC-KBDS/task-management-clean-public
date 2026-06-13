"""Named constants for the feedback-target scoring gate.

The reconciler (and the operating agent) score pending proposals against an incoming
feedback clause to decide which proposal, if any, the feedback targets.  The gate that
guards that decision is intentionally strict and must not drift: a candidate is only
accepted when its score clears ``MIN_TARGET_SCORE`` *and* beats the runner-up by at
least ``MIN_RUNNER_UP_GAP``.  Those two values are a hard invariant of the system; this
module exists to give them (and the scoring weights) names instead of leaving the magic
numbers inline at the call sites.
"""

from __future__ import annotations

from datetime import datetime
from typing import TypeVar

# --- The scoring gate (HARD INVARIANT: score >= 8 AND runner-up gap >= 3) ---
MIN_TARGET_SCORE = 8
MIN_RUNNER_UP_GAP = 3

# --- Token-overlap weights (reconciler profile) ---
TITLE_TOKEN_WEIGHT = 12
RAW_TOKEN_WEIGHT = 5
CONTEXT_TOKEN_WEIGHT = 4

# --- Bonus weights for specific feedback signals ---
DEICTIC_CONFLICT_BONUS = 80
DISCUSSION_WORD_BONUS = 10
NEXT_WEEK_BONUS = 25
RETURN_BONUS = 20
SLOT_HINT_BONUS = 3


_Candidate = TypeVar("_Candidate")


def pick_best_candidate(
    scored: list[tuple[int, _Candidate]],
    *,
    min_score: int = MIN_TARGET_SCORE,
    min_gap: int = MIN_RUNNER_UP_GAP,
) -> _Candidate | None:
    """Select the highest-scoring candidate that clears the scoring gate.

    ``scored`` is a list of ``(score, proposal)`` tuples (scores already filtered to
    ``> 0`` by the caller).  Candidates are ordered by ``(score, recency)`` where recency
    is the proposal's ``updated_at`` (falling back to ``created_at``).  The best candidate
    is returned only when its score reaches ``min_score`` and beats the runner-up by at
    least ``min_gap``; otherwise the target is considered ambiguous and ``None`` is returned.
    """

    if not scored:
        return None
    ordered = sorted(
        scored,
        key=lambda item: (
            item[0],
            getattr(item[1], "updated_at", None) or getattr(item[1], "created_at", None) or datetime.min,
        ),
        reverse=True,
    )
    best_score, best = ordered[0]
    runner_up = ordered[1][0] if len(ordered) > 1 else 0
    if best_score < min_score:
        return None
    if runner_up and best_score - runner_up < min_gap:
        return None
    return best
