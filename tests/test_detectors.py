"""Tests for detectors — pure functions, no cluster needed."""

from self_healer import detectors


def make_pod(name="p", namespace="default", **status_overrides):
    status = {"phase": "Running", "containerStatuses": []}
    status.update(status_overrides)
    return {
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace},
        "status": status,
    }


def container(name="c", restarts=0, waiting_reason=None, terminated_reason=None):
    cs = {"name": name, "restartCount": restarts, "ready": True, "state": {}}
    if waiting_reason:
        cs["state"] = {"waiting": {"reason": waiting_reason}}
        cs["ready"] = False
    elif terminated_reason:
        cs["state"] = {
            "terminated": {"reason": terminated_reason, "exitCode": 1}
        }
        cs["ready"] = False
    else:
        cs["state"] = {"running": {}}
    return cs


def test_crashloop_detected():
    pod = make_pod(
        containerStatuses=[container(restarts=12, waiting_reason="CrashLoopBackOff")]
    )
    findings = detectors.detect_crashloop(pod)
    assert len(findings) == 1
    f = findings[0]
    assert f["detector"] == "crashloop"
    assert f["severity"] == "high"
    assert f["name"] == "p"
    assert "12 restarts" in f["detail"]


def test_crashloop_below_threshold_ignored():
    pod = make_pod(
        containerStatuses=[container(restarts=1, waiting_reason="CrashLoopBackOff")]
    )
    assert detectors.detect_crashloop(pod) == []


def test_crashloop_healthy_pod_clean():
    pod = make_pod(containerStatuses=[container(restarts=0)])
    assert detectors.detect_crashloop(pod) == []


def test_crashloop_ignores_non_pods():
    dep = {"kind": "Deployment", "metadata": {"name": "d"}}
    assert detectors.detect_crashloop(dep) == []


def test_oomkilled_current_state():
    pod = make_pod(containerStatuses=[container(terminated_reason="OOMKilled")])
    findings = detectors.detect_oomkilled(pod)
    assert len(findings) == 1
    assert findings[0]["detector"] == "oomkilled"
    assert findings[0]["severity"] == "high"


def test_oomkilled_last_state():
    cs = container()
    cs["lastState"] = {"terminated": {"reason": "OOMKilled", "exitCode": 137}}
    cs["state"] = {"running": {}}
    pod = make_pod(containerStatuses=[cs])
    assert len(detectors.detect_oomkilled(pod)) == 1


def test_oomkilled_normal_exit_not_flagged():
    pod = make_pod(containerStatuses=[container(terminated_reason="Completed")])
    assert detectors.detect_oomkilled(pod) == []


def test_failed_job_past_backoff():
    job = {
        "kind": "Job",
        "metadata": {"name": "j", "namespace": "default"},
        "spec": {"backoffLimit": 3},
        "status": {
            "failed": 4,
            "conditions": [
                {
                    "type": "Failed",
                    "status": "True",
                    "reason": "BackoffLimitExceeded",
                }
            ],
        },
    }
    findings = detectors.detect_failed_job(job)
    assert len(findings) == 1
    assert findings[0]["detector"] == "failed_job"


def test_failed_job_still_retrying_not_flagged():
    job = {
        "kind": "Job",
        "metadata": {"name": "j"},
        "spec": {"backoffLimit": 6},
        "status": {"failed": 2, "conditions": []},
    }
    assert detectors.detect_failed_job(job) == []


def test_pending_unschedulable():
    pod = make_pod(
        phase="Pending",
        containerStatuses=[],
        conditions=[
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": "0/3 nodes available: Insufficient memory",
            }
        ],
    )
    findings = detectors.detect_pending_pod(pod)
    assert len(findings) == 1
    assert findings[0]["detector"] == "pending"
    assert findings[0]["severity"] == "medium"


def test_pending_scheduling_in_progress_not_flagged():
    pod = make_pod(phase="Pending", containerStatuses=[], conditions=[])
    assert detectors.detect_pending_pod(pod) == []


def test_failed_rollout_deadline_exceeded():
    dep = {
        "kind": "Deployment",
        "metadata": {"name": "d", "namespace": "default"},
        "spec": {"replicas": 3},
        "status": {
            "replicas": 3,
            "updatedReplicas": 1,
            "unavailableReplicas": 2,
            "conditions": [
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                }
            ],
        },
    }
    findings = detectors.detect_failed_rollout(dep)
    assert len(findings) == 1
    assert findings[0]["detector"] == "failed_rollout"


def test_failed_rollout_stalled_without_deadline():
    dep = {
        "kind": "Deployment",
        "metadata": {"name": "d"},
        "spec": {"replicas": 3},
        "status": {
            "replicas": 3,
            "updatedReplicas": 1,
            "unavailableReplicas": 2,
            "conditions": [],
        },
    }
    assert len(detectors.detect_failed_rollout(dep)) == 1


def test_healthy_rollout_clean():
    dep = {
        "kind": "Deployment",
        "metadata": {"name": "d"},
        "spec": {"replicas": 2},
        "status": {
            "replicas": 2,
            "updatedReplicas": 2,
            "conditions": [
                {"type": "Progressing", "status": "True"},
            ],
        },
    }
    assert detectors.detect_failed_rollout(dep) == []


def test_scan_runs_all_detectors():
    crash = make_pod(
        "crash",
        containerStatuses=[container(restarts=9, waiting_reason="CrashLoopBackOff")],
    )
    ok = make_pod("ok", containerStatuses=[container()])
    findings = detectors.scan([crash, ok])
    assert len(findings) == 1
    assert findings[0]["name"] == "crash"
