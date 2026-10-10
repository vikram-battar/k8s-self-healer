"""Tests for the remediation engine: dry-run vs live, cooldowns,
circuit breaker, audit trail, and the never-act-silently rule."""

from self_healer import policies, remediations
from self_healer.audit import AuditLog
from self_healer.clients import DryRunClient, FakeClient


def make_policies():
    _, plist = policies.parse(
        {
            "global": {"max_actions_per_hour": 20, "default_cooldown_minutes": 15},
            "policies": [
                {
                    "name": "restart-crashloop",
                    "match": {"detector": "crashloop"},
                    "action": {"type": "delete_pod"},
                    "cooldown_minutes": 30,
                }
            ],
        }
    )
    return plist


def crash_finding(name="p", namespace="default"):
    return {
        "detector": "crashloop",
        "severity": "high",
        "kind": "Pod",
        "namespace": namespace,
        "name": name,
        "detail": "container c in CrashLoopBackOff (9 restarts)",
    }


def run_engine(findings, plist, client, dry_run=True, max_actions=20):
    safety = remediations.SafetyState(max_actions_per_hour=max_actions)
    audit = AuditLog()
    decisions = remediations.remediate(
        findings, plist, client, safety, audit, dry_run=dry_run
    )
    return decisions, client, audit


def test_dry_run_records_but_mutates_nothing():
    plist = make_policies()
    client = FakeClient(
        {("Pod", "default", "p"): {"kind": "Pod", "metadata": {"name": "p"}}},
        dry_run=True,
    )
    decisions, _, audit = run_engine([crash_finding()], plist, client, dry_run=True)
    assert decisions[0]["outcome"] == "dry_run"
    # recorded, but the fake cluster is untouched
    assert ("Pod", "default", "p") in client.objects
    assert client.calls[0]["method"] == "delete_pod"
    assert audit.records[0]["outcome"] == "dry_run"


def test_live_mode_mutates_fake_client():
    plist = make_policies()
    client = FakeClient(
        {("Pod", "default", "p"): {"kind": "Pod", "metadata": {"name": "p"}}}
    )
    decisions, _, audit = run_engine([crash_finding()], plist, client, dry_run=False)
    assert decisions[0]["outcome"] == "acted"
    assert ("Pod", "default", "p") not in client.objects  # pod deleted
    assert audit.records[0]["policy"] == "restart-crashloop"


def test_never_acts_silently():
    plist = make_policies()
    client = FakeClient()
    run_engine([crash_finding()], plist, client, dry_run=False)
    methods = [c["method"] for c in client.calls]
    assert "annotate" in methods
    assert "emit_event" in methods
    assert len(client.events) == 1
    assert client.events[0]["reason"] == "SelfHealerRemediation"


def test_cooldown_skips_repeat_action():
    plist = make_policies()
    client = FakeClient()
    safety = remediations.SafetyState(max_actions_per_hour=20)
    audit = AuditLog()
    d1 = remediations.remediate(
        [crash_finding()], plist, client, safety, audit, dry_run=False
    )
    d2 = remediations.remediate(
        [crash_finding()], plist, client, safety, audit, dry_run=False
    )
    assert d1[0]["outcome"] == "acted"
    assert d2[0]["outcome"] == "skipped_cooldown"


def test_circuit_breaker_stops_the_bleeding():
    plist = make_policies()
    client = FakeClient()
    findings = [crash_finding(name="p%d" % i) for i in range(5)]
    decisions, _, audit = run_engine(
        findings, plist, client, dry_run=False, max_actions=2
    )
    outcomes = [d["outcome"] for d in decisions]
    assert outcomes.count("acted") == 2
    assert outcomes.count("skipped_breaker") == 3


def test_no_matching_policy_recorded():
    plist = make_policies()
    client = FakeClient()
    finding = dict(crash_finding())
    finding["detector"] = "pending"  # no policy matches this
    decisions, _, audit = run_engine([finding], plist, client, dry_run=True)
    assert decisions[0]["outcome"] == "no_matching_policy"


def test_scale_action_passes_replicas():
    _, plist = policies.parse(
        {
            "policies": [
                {
                    "name": "scale-up",
                    "match": {"detector": "pending"},
                    "action": {"type": "scale_deployment", "replicas": 5},
                }
            ]
        }
    )
    client = FakeClient(
        {
            ("Deployment", "default", "d"): {
                "kind": "Deployment",
                "metadata": {"name": "d", "namespace": "default"},
                "spec": {"replicas": 1},
            }
        }
    )
    finding = {
        "detector": "pending",
        "severity": "medium",
        "kind": "Deployment",
        "namespace": "default",
        "name": "d",
        "detail": "unschedulable",
    }
    decisions, _, _ = run_engine([finding], plist, client, dry_run=False)
    assert decisions[0]["outcome"] == "acted"
    assert client.objects[("Deployment", "default", "d")]["spec"]["replicas"] == 5
