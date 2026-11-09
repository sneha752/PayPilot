"""Deterministic policy layer — the only component that authorizes spending."""

from .policy_engine import evaluate_policy

__all__ = ["evaluate_policy"]
