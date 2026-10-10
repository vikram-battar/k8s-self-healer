"""Tests for the diagnosis bundler — the honest AIOps bridge."""

import json

from self_healer import bundler


def workload(name="api", namespace="default", kind="Deployment"):
    return {
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": "api"},
        },
    }


def pod(name, phase="Running", container_state="running", restarts=0):
    return {
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "default",
            "labels": {"app": "api"},
        },
        "status": {
            "phase": phase,
            "containerStatuses": [
                {
                    "name": "api",
                    "state": {container_state: {}},
                    "restartCount": restarts,
                    "ready": container_state == "running",
                }
            ],
        },
    }


def test_bundle_structure():
    w = workload()
    objects = [w, pod("api-abc-1"), pod("api-abc-2")]
    b = bundler.build_bundle(w, objects, findings=[])
    assert b["schema"] == "self-healer/diagnosis-bundle/v1"
    assert b["workload"]["name"] == "api"
    assert b["workload"]["namespace"] == "default"
    assert len(b["pods"]) == 2
    assert "llm_prompt" in b
    assert b["collected_at"].endswith("Z")


def test_bundle_matches_pods_by_name_prefix():
    w = workload(name="api")
    objects = [w, pod("api-xyz-1"), pod("other-xyz-1",)]
    # other-xyz-1 has app=api label too — matched via labels as well;
    # a truly unrelated pod should be excluded:
    objects.append(
        {
            "kind": "Pod",
            "metadata": {
                "name": "unrelated-1",
                "namespace": "default",
                "labels": {"app": "other"},
            },
            "status": {"phase": "Running", "containerStatuses": []},
        }
    )
    b = bundler.build_bundle(w, objects, findings=[])
    names = {p["name"] for p in b["pods"]}
    assert "api-xyz-1" in names
    assert "unrelated-1" not in names


def test_bundle_includes_findings_and_events():
    w = workload()
    finding = {
        "detector": "crashloop",
        "severity": "high",
        "detail": "container api in CrashLoopBackOff (9 restarts)",
    }
    event = {
        "kind": "Event",
        "metadata": {"namespace": "default"},
        "involvedObject": {"kind": "Pod", "name": "api-abc-1"},
        "reason": "BackOff",
        "message": "back-off restarting failed container",
        "type": "Warning",
        "count": 12,
        "lastTimestamp": "2026-10-10T06:00:00Z",
    }
    objects = [w, pod("api-abc-1"), event]
    b = bundler.build_bundle(w, objects, findings=[finding])
    assert b["findings"] == [finding]
    assert len(b["recent_events"]) == 1
    assert b["recent_events"][0]["reason"] == "BackOff"
    prompt = b["llm_prompt"]
    assert "CrashLoopBackOff" in prompt
    assert "BackOff" in prompt
    assert "api-abc-1" in prompt


def test_bundle_auto_runs_detectors():
    w = workload()
    bad = pod("api-abc-1")
    bad["status"]["containerStatuses"][0]["state"] = {
        "waiting": {"reason": "CrashLoopBackOff"}
    }
    bad["status"]["containerStatuses"][0]["restartCount"] = 9
    bad["status"]["containerStatuses"][0]["ready"] = False
    b = bundler.build_bundle(w, [w, bad])  # no findings passed
    detectors_hit = {f["detector"] for f in b["findings"]}
    assert "crashloop" in detectors_hit


def test_bundle_serializes_to_json():
    w = workload()
    b = bundler.build_bundle(w, [w, pod("api-abc-1")], findings=[])
    text = bundler.bundle_to_json(b)
    parsed = json.loads(text)
    assert parsed["workload"]["name"] == "api"
