"""
Общий проверяемый оценщик v2; исторический находится в
legacy_evaluator.
"""

from scripts.evaluation import evaluate_plan, read_result
from scripts.validation import validate_day, validate_result

__all__ = ["evaluate_plan", "read_result", "validate_day", "validate_result"]
