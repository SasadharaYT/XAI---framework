from governance_sdk.models.action import EvaluationResult


def test_expanded_evaluation_and_legacy_response():
    assert EvaluationResult.model_validate({'outcome': 'approval_required'}).risk_level is None
    result = EvaluationResult.model_validate({
        'outcome': 'deny', 'risk_level': 'high', 'reason': 'Blocked operation',
        'reason_code': 'RULE_MATCHED', 'policy_version': 'v1',
        'matched_rule': {'rule_id': 'blocked', 'effect': 'deny', 'priority': 1},
        'approval_id': None, 'authorization_receipt': None,
    })
    assert result.outcome == 'deny'
    assert result.policy_version == 'v1'
