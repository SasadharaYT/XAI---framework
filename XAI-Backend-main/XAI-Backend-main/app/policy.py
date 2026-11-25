"""Deterministic, configuration-driven policy evaluation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


OUTCOMES = {"allow", "approval_required", "deny"}
RISK_LEVELS = {"low", "medium", "high", "critical"}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class PolicyResult:
    outcome: str
    risk_level: str
    reason: str
    reason_code: str
    matched_rule: dict[str, Any] | None
    policy_version: str
    evidence: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "matched_rule": self.matched_rule,
            "policy_version": self.policy_version,
            "evidence": self.evidence,
        }


def _default_policy() -> dict[str, Any]:
    return {"version": "default", "default_outcome": "approval_required", "rules": []}


def load_policy() -> tuple[dict[str, Any], bool]:
    """Load a project-independent policy from JSON, failing closed on bad config."""
    raw = os.getenv("GOVERNANCE_POLICY_JSON", "")
    if not raw:
        return _default_policy(), True
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("rules", []), list):
            raise ValueError
        version = value.get("version", "configured")
        if not isinstance(version, str) or not version:
            raise ValueError
        default = value.get("default_outcome", "approval_required")
        if default != "approval_required":
            raise ValueError
        return value, True
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"version": "invalid", "default_outcome": "approval_required", "rules": []}, False


def _valid_risk(value: Any) -> str | None:
    value = str(value or "").lower()
    return value if value in RISK_LEVELS else None


def _risk(project: Any, agent: Any) -> str:
    values = [_valid_risk(getattr(project, "risk", None)), _valid_risk(getattr(agent, "risk", None))]
    values = [value for value in values if value]
    return max(values, key=RISK_ORDER.get) if values else "medium"


def _matches(rule: dict[str, Any], context: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    conditions = rule.get("when", {})
    if not isinstance(conditions, dict):
        return False, []
    evidence: list[dict[str, Any]] = []
    direct = {
        "action_name": (context.get("action_name"), "caller"),
        "agent_id": (context.get("agent_id"), "backend"),
        "agent_status": (context.get("agent_status"), "backend"),
        "project_risk": (context.get("project_risk"), "backend"),
        "agent_risk": (context.get("agent_risk"), "backend"),
    }
    for field, expected in conditions.items():
        if field in direct:
            actual, source = direct[field]
            expected_values = expected if isinstance(expected, list) else [expected]
            if actual not in expected_values:
                return False, []
            evidence.append({"field": field, "source": source, "value": actual})
        elif field == "action_prefix":
            actual = str(context.get("action_name", ""))
            if not actual.startswith(str(expected)):
                return False, []
            evidence.append({"field": field, "source": "caller", "value": actual})
        elif field == "metadata_equals":
            if not isinstance(expected, dict):
                return False, []
            metadata = context.get("metadata", {})
            if any(metadata.get(key) != value for key, value in expected.items()):
                return False, []
            evidence.extend({"field": f"metadata.{key}", "source": "caller", "value": metadata.get(key)} for key in expected)
        elif field == "parameter_equals":
            if not isinstance(expected, dict):
                return False, []
            parameters = context.get("action_parameters", {})
            if any(parameters.get(key) != value for key, value in expected.items()):
                return False, []
            evidence.extend({"field": f"action_parameters.{key}", "source": "caller", "value": parameters.get(key)} for key in expected)
        else:
            return False, []
    return True, evidence


def evaluate(*, project: Any, agent: Any, action_name: str, action_parameters: dict[str, Any], metadata: dict[str, Any]) -> PolicyResult:
    policy, valid = load_policy()
    policy_version = str(policy.get("version", "default"))
    risk_level = _risk(project, agent)
    context = {
        "action_name": action_name,
        "action_parameters": action_parameters,
        "metadata": metadata,
        "agent_id": getattr(agent, "id", None),
        "agent_status": str(getattr(agent, "status", "unknown")).lower(),
        "project_risk": str(getattr(project, "risk", "medium")).lower(),
        "agent_risk": str(getattr(agent, "risk", "medium")).lower(),
    }
    candidates: list[tuple[int, int, int, str, dict[str, Any], list[dict[str, Any]]]] = []
    for rule in policy.get("rules", []):
        if not isinstance(rule, dict) or rule.get("effect") not in OUTCOMES:
            continue
        matched, evidence = _matches(rule, context)
        if matched:
            effect = rule["effect"]
            rank = {"deny": 0, "approval_required": 1, "allow": 2}[effect]
            specificity = len(rule.get("when", {})) if isinstance(rule.get("when", {}), dict) else 0
            if effect == "allow" and not any(item["source"] == "backend" for item in evidence):
                continue
            candidates.append((rank, -specificity, -int(rule.get("priority", 0)), str(rule.get("rule_id", "")), rule, evidence))
    if not candidates:
        reason = "Policy configuration is invalid; human review is required" if not valid else "No policy rule matched; human review is required"
        return PolicyResult("approval_required", risk_level, reason, "POLICY_UNAVAILABLE" if not valid else "NO_MATCH", None, policy_version, [])
    _, _, _, _, rule, evidence = sorted(candidates, key=lambda item: item[:4])[0]
    effect = str(rule["effect"])
    rule_risk = _valid_risk(rule.get("risk"))
    if rule_risk:
        risk_level = max(risk_level, rule_risk, key=RISK_ORDER.get)
    return PolicyResult(
        effect,
        risk_level,
        str(rule.get("reason", rule.get("description", "Policy rule matched"))),
        str(rule.get("reason_code", rule.get("rule_id", "POLICY_RULE_MATCHED"))),
        {"rule_id": str(rule.get("rule_id", "")), "effect": effect, "priority": int(rule.get("priority", 0)), "description": rule.get("description")},
        policy_version,
        evidence,
    )
