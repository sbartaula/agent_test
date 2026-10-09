"""Evaluation suite: realistic issues with an explicit expected outcome per case.

Offline mode replays scripted model answers, so it tests the *harness* (verification, limits,
permissions, refusal of dangerous edits) deterministically and for free. Live mode runs the same
repositories through the real model and checks the same expected outcomes.
"""

from issuepilot.evals.cases import CASES, Case
from issuepilot.evals.runner import CaseResult, run_case, run_suite

__all__ = ["CASES", "Case", "CaseResult", "run_case", "run_suite"]
