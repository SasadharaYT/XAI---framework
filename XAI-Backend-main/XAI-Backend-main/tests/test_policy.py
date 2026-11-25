from types import SimpleNamespace

from app.policy import evaluate


def test_no_match_requires_approval():
    project = SimpleNamespace(risk="Low")
    agent = SimpleNamespace(id="agent-1", risk="Low", status="Active")
    result = evaluate(project=project, agent=agent, action_name="read", action_parameters={}, metadata={})
    assert result.outcome == "approval_required"
    assert result.reason_code == "NO_MATCH"


def test_block_precedes_allow(monkeypatch):
    monkeypatch.setenv(
        "GOVERNANCE_POLICY_JSON",
        '{"version":"v1","rules":['
        '{"rule_id":"allow-read","effect":"allow","when":{"action_name":"read"}},'
        '{"rule_id":"block-read","effect":"deny","priority":2,"when":{"action_name":"read"}}]}',
    )
    project = SimpleNamespace(risk="Low")
    agent = SimpleNamespace(id="agent-1", risk="Low", status="Active")
    result = evaluate(project=project, agent=agent, action_name="read", action_parameters={}, metadata={})
    assert result.outcome == "deny"
    assert result.matched_rule["rule_id"] == "block-read"


def test_allow_rule_records_caller_evidence(monkeypatch):
    monkeypatch.setenv(
        "GOVERNANCE_POLICY_JSON",
        '{"version":"v2","rules":['
        '{"rule_id":"allow-read","effect":"allow","when":{"action_name":"read","agent_status":"active","metadata_equals":{"environment":"test"}}}]}',
    )
    project = SimpleNamespace(risk="Low")
    agent = SimpleNamespace(id="agent-1", risk="Low", status="Active")
    result = evaluate(project=project, agent=agent, action_name="read", action_parameters={}, metadata={"environment": "test"})
    assert result.outcome == "allow"
    assert result.policy_version == "v2"
    assert any(item["field"] == "metadata.environment" for item in result.evidence)


def test_allow_rule_with_only_caller_evidence_falls_back_to_approval(monkeypatch):
    monkeypatch.setenv(
        "GOVERNANCE_POLICY_JSON",
        '{"version":"v3","rules":['
        '{"rule_id":"unsafe-allow","effect":"allow","when":{"metadata_equals":{"environment":"test"}}}]}',
    )
    project = SimpleNamespace(risk="Low")
    agent = SimpleNamespace(id="agent-1", risk="Low", status="Active")
    result = evaluate(project=project, agent=agent, action_name="read", action_parameters={}, metadata={"environment": "test"})
    assert result.outcome == "approval_required"
