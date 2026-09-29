"""Abstract base class for Quality Gate decision strategies.

Every strategy (S1–S4) must subclass StrategyBase and implement run() and decide().
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import (
    EntityIssue,
    FixAttempt,
    GateReport,
    QualityGateInput,
    StrategyDecision,
)


class StrategyBase(ABC):
    """Contract that all Quality Gate strategies must satisfy."""

    name: str  # "s1_thresholds", "s2_shadow", "s3_convergence", "s4_hybrid"

    @abstractmethod
    def run(self, input: QualityGateInput) -> GateReport:
        """Execute the full gate pass: detect issues, attempt fixes, decide outcomes.

        Returns a GateReport with verdict, issues, fixes, and route-backs.
        """
        ...

    @abstractmethod
    def decide(self, issue: EntityIssue, proposed_fix: FixAttempt) -> StrategyDecision:
        """Decide what to do with a single issue + proposed fix.

        Returns:
            StrategyDecision with one of:
              - DecisionType.COMMIT  — apply fix to prod
              - DecisionType.REJECT  — don't apply, reroute entity to origin phase
              - DecisionType.DEFER   — retry next pass (S3 convergence only)
        """
        ...
