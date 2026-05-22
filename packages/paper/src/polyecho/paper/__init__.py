"""polyecho.paper -- forward paper-trading ladder worker (C1).

A *validation* layer: the worker shadow-copies leader trades into a
simulated ``paper_session`` and advances a six-state lifecycle ladder
(discovered -> candidate -> paper -> trial_ready, with cooldown / retired
branches).  It NEVER auto-flips a leader to real money -- ``trial_ready``
is advisory only.

Public API surface
------------------
* :class:`PaperWorker`         -- orchestrates one paper-trading cycle.
* :class:`PaperRepo`           -- the DB-access layer (mocked in tests).
* :func:`advance`              -- the pure lifecycle state machine.
* :class:`LadderCandidate`     -- the immutable snapshot the ladder reads.
* :class:`LifecycleState`      -- the six ladder states.
* :class:`Transition`          -- one ladder-step result.
* :func:`recompute_summary`    -- recompute the paper_sessions summary.
* :class:`PaperSafetyViolation`-- raised on any real-money write attempt.
"""

from polyecho.paper.safety import PaperSafetyViolation, assert_no_real_money_write
from polyecho.paper.state_machine import (
    LadderCandidate,
    LifecycleState,
    Transition,
    advance,
)
from polyecho.paper.summary import PaperFill, SessionSummary, recompute_summary
from polyecho.paper.worker import PaperRepo, PaperWorker

__all__ = [
    "LadderCandidate",
    "LifecycleState",
    "PaperFill",
    "PaperRepo",
    "PaperSafetyViolation",
    "PaperWorker",
    "SessionSummary",
    "Transition",
    "advance",
    "assert_no_real_money_write",
    "recompute_summary",
]
