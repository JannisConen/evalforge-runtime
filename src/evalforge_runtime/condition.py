"""Shared condition evaluator — used by both ActionRunner and trigger filters."""

from __future__ import annotations

import logging
import re as _re
from typing import Any

logger = logging.getLogger(__name__)


def evaluate_condition(
    condition: dict[str, Any],
    data: dict[str, Any],
    *,
    fn_name: str = "should_run",
) -> bool:
    """Evaluate a condition (always/visual/python) against a data dict.

    Args:
        condition: Condition dict with 'mode', 'rules', 'logic', 'code' keys.
        data: The data dict to evaluate against (output fields or trigger item data).
        fn_name: Expected Python function name for python mode.
    """
    mode = condition.get("mode", "always")
    if mode == "always":
        return True
    if mode == "visual":
        return _eval_visual(condition, data)
    if mode == "python":
        return _eval_python(condition, data, fn_name=fn_name)
    return True


def _eval_visual(condition: dict[str, Any], data: dict[str, Any]) -> bool:
    """Evaluate grouped visual rules against data fields."""
    groups = condition.get("groups", [])
    if not groups:
        return True
    group_logic = condition.get("group_logic", "and")
    results = [_eval_group(g, data) for g in groups]
    return all(results) if group_logic == "and" else any(results)


def _eval_group(group: dict[str, Any], data: dict[str, Any]) -> bool:
    """Evaluate a single rule group."""
    rules = group.get("rules", [])
    logic = group.get("logic", "and")
    if not rules:
        return True
    results = [_eval_rule(rule, data) for rule in rules]
    return all(results) if logic == "and" else any(results)


def _eval_rule(rule: dict[str, Any], data: dict[str, Any]) -> bool:
    """Evaluate a single visual rule."""
    field = rule.get("field", "")
    operator = rule.get("operator", "equals")
    expected = rule.get("value", "")

    # Resolve field value (supports dot paths)
    actual: Any = data
    for part in field.split("."):
        if isinstance(actual, dict):
            actual = actual.get(part)
        else:
            actual = None
            break

    actual_str = str(actual) if actual is not None else ""

    if operator == "equals":
        return actual_str == expected
    if operator == "not_equals":
        return actual_str != expected
    if operator == "contains":
        return expected in actual_str
    if operator == "not_contains":
        return expected not in actual_str
    if operator == "is_empty":
        return actual is None or actual_str == "" or actual == []
    if operator == "is_not_empty":
        return actual is not None and actual_str != "" and actual != []
    if operator == "matches":
        return _glob_match(actual_str, expected)
    if operator == "regex":
        try:
            return bool(_re.search(expected, actual_str))
        except _re.error:
            return False
    if operator in ("gt", "lt", "gte", "lte"):
        try:
            a = float(actual_str)
            b = float(expected)
            if operator == "gt":
                return a > b
            if operator == "lt":
                return a < b
            if operator == "gte":
                return a >= b
            if operator == "lte":
                return a <= b
        except (ValueError, TypeError):
            return False

    return True


def _glob_match(value: str, pattern: str) -> bool:
    """Simple glob match supporting * wildcards. Case-insensitive."""
    escaped = _re.escape(pattern.lower()).replace(r"\*", ".*")
    return bool(_re.fullmatch(escaped, value.lower()))


def _eval_python(
    condition: dict[str, Any],
    data: dict[str, Any],
    *,
    fn_name: str = "should_run",
) -> bool:
    """Execute user Python condition code.

    The code must define a function matching fn_name that accepts a single dict arg.
    """
    code = condition.get("code", "")
    if not code.strip():
        return True

    namespace: dict[str, Any] = {}
    try:
        exec(code, namespace)
        fn = namespace.get(fn_name)
        if fn and callable(fn):
            result = fn(data)
            return bool(result)
        return True
    except Exception as e:
        logger.warning("Python condition evaluation failed (fn=%s): %s", fn_name, e)
        return True  # fail-open
