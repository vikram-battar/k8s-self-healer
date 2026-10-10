"""Tests for policy loading, validation, and matching."""

import pytest

from self_healer import policies


def test_parse_defaults():
    global_cfg, plist = policies.parse({"policies": []})
    assert global_cfg["dry_run"] is True
    assert global_cfg["max_actions_per_hour"] == 20
    assert global_cfg["default_cooldown_minutes"] == 15
    assert plist == []


def test_parse_policy_inherits_defaults():
    _, plist = policies.parse(
        {
            "policies": [
                {
                    "name": "p",
                    "match": {"detector": "crashloop"},
                    "action": {"type": "delete_pod"},
                }
            ]
        }
    )
    assert plist[0]["cooldown_minutes"] == 15
    assert plist[0]["namespaces"] is None
    assert plist[0]["exclude_namespaces"] == []


def test_parse_policy_explicit_values():
    _, plist = policies.parse(
        {
            "global": {"default_cooldown_minutes": 5},
            "policies": [
                {
                    "name": "p",
                    "match": {"detector": "failed_rollout"},
                    "action": {"type": "rollback_deployment"},
                    "cooldown_minutes": 60,
                    "namespaces": ["default"],
                    "exclude_namespaces": ["kube-system"],
                }
            ],
        }
    )
    p = plist[0]
    assert p["cooldown_minutes"] == 60
    assert p["namespaces"] == ["default"]
    assert p["exclude_namespaces"] == ["kube-system"]


def test_unknown_action_rejected():
    with pytest.raises(ValueError, match="unknown action type"):
        policies.parse(
            {
                "policies": [
                    {
                        "name": "p",
                        "match": {"detector": "crashloop"},
                        "action": {"type": "nuke_everything"},
                    }
                ]
            }
        )


def test_missing_name_rejected():
    with pytest.raises(ValueError, match="missing 'name'"):
        policies.parse(
            {
                "policies": [
                    {"match": {"detector": "crashloop"}, "action": {"type": "delete_pod"}}
                ]
            }
        )


def test_scale_needs_replicas():
    with pytest.raises(ValueError, match="needs action.replicas"):
        policies.parse(
            {
                "policies": [
                    {
                        "name": "p",
                        "match": {"detector": "pending"},
                        "action": {"type": "scale_deployment"},
                    }
                ]
            }
        )


def test_load_example_policies():
    global_cfg, plist = policies.load("examples/policies.yaml")
    assert len(plist) == 4
    assert global_cfg["dry_run"] is True
    names = {p["name"] for p in plist}
    assert names == {
        "restart-crashloop-pods",
        "restart-oomkilled-pods",
        "rollback-failed-deployments",
        "annotate-failed-jobs",
    }


def test_matching_policies_detector_and_namespace():
    _, plist = policies.parse(
        {
            "policies": [
                {
                    "name": "a",
                    "match": {"detector": "crashloop"},
                    "action": {"type": "delete_pod"},
                    "exclude_namespaces": ["kube-system"],
                },
                {
                    "name": "b",
                    "match": {"detector": "oomkilled"},
                    "action": {"type": "delete_pod"},
                },
            ]
        }
    )
    finding = {"detector": "crashloop", "namespace": "default"}
    matched = policies.matching_policies(finding, plist)
    assert [p["name"] for p in matched] == ["a"]

    finding_sys = {"detector": "crashloop", "namespace": "kube-system"}
    assert policies.matching_policies(finding_sys, plist) == []

    finding_ns = {"detector": "crashloop", "namespace": "default"}
    # allowlist case
    _, plist2 = policies.parse(
        {
            "policies": [
                {
                    "name": "c",
                    "match": {"detector": "crashloop"},
                    "action": {"type": "delete_pod"},
                    "namespaces": ["staging"],
                }
            ]
        }
    )
    assert policies.matching_policies(finding_ns, plist2) == []
    assert (
        policies.matching_policies(
            {"detector": "crashloop", "namespace": "staging"}, plist2
        )[0]["name"]
        == "c"
    )
