"""Deterministic rule engine and validated project policy input schema."""
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Outcome = Literal['allow', 'approval_required', 'deny']
Risk = Literal['low', 'medium', 'high', 'critical']


class Rule(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    rule_id: str = Field(min_length=1)
    outcome: Outcome
    risk_level: Risk
    reason: str = Field(min_length=1)
    action_name: str = Field(min_length=1)
    project_id: str | None = None
    agent_id: str | None = None
    priority: int = 0
    enabled: bool = True


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: str = Field(min_length=1)
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique_ids(self):
        if len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ValueError('Duplicate rule IDs')
        return self


def load_policy() -> Policy:
    raw = os.getenv('GOVERNANCE_POLICY_JSON')
    if not raw:
        return Policy(version='default')
    try:
        return Policy.model_validate_json(raw)
    except ValidationError:
        return Policy(version='invalid')


class ProjectPolicyIn(Policy):
    """Full policy replacement; tenant identity comes exclusively from the route."""
    version: str = Field(min_length=1, max_length=120, pattern=r'\S')
    enabled: bool = True

    @model_validator(mode='after')
    def nonblank_rule_fields(self):
        for rule in self.rules:
            if any(not value.strip() for value in (rule.rule_id, rule.action_name, rule.reason)):
                raise ValueError('Rule IDs, actions and reasons must not be blank')
        return self


def evaluate(policy: Policy, *, action_name: str, project_id: str, agent_id: str) -> dict:
    # Identity is resolved from backend records. Reason/metadata are deliberately
    # not arguments: a caller cannot claim a privileged context to gain ALLOW.
    matches = [r for r in policy.rules if r.enabled and r.action_name == action_name
               and (r.project_id is None or r.project_id == project_id)
               and (r.agent_id is None or r.agent_id == agent_id)]
    matches.sort(key=lambda r: (
        {'deny': 0, 'approval_required': 1, 'allow': 2}[r.outcome],
        -(int(r.project_id is not None) + int(r.agent_id is not None)),
        -r.priority, r.rule_id,
    ))
    rule = matches[0] if matches else None
    return {
        'outcome': rule.outcome if rule else 'approval_required',
        'risk_level': rule.risk_level if rule else 'medium',
        'reason': rule.reason if rule else 'No applicable valid rule; human review is required.',
        'reason_code': 'RULE_MATCHED' if rule else 'NO_MATCH',
        'matched_rule': ({'rule_id': rule.rule_id, 'effect': rule.outcome,
                          'priority': rule.priority} if rule else None),
        'policy_version': policy.version,
        'approval_id': None,
        'authorization_receipt': None,
    }
