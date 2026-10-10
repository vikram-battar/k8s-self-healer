"""Detectors: pure functions over Kubernetes API objects (plain dicts).

Each detector takes one k8s object dict — exactly what the API server
returns, or what you load from a snapshot file — and returns a list of
finding dicts. No cluster access, no side effects, no mocks needed to
test them.

A finding looks like:
    {
        "detector": "crashloop",
        "severity": "high",
        "kind": "Pod",
        "namespace": "default",
        "name": "api-7d9f8c6b5d-x2kq9",
        "container": "api",
        "detail": "container api in CrashLoopBackOff (12 restarts)",
    }
"""

CRASHLOOP_RESTART_THRESHOLD = 3


def _meta(obj):
    return obj.get("metadata", {}) or {}


def _namespace(obj):
    return _meta(obj).get("namespace", "default")


def _name(obj):
    return _meta(obj).get("name")


def _container_statuses(pod):
    return pod.get("status", {}).get("containerStatuses", []) or []


def _all_container_statuses(pod):
    """Current plus init container statuses."""
    status = pod.get("status", {}) or {}
    return (status.get("containerStatuses", []) or []) + (
        status.get("initContainerStatuses", []) or []
    )


def detect_crashloop(pod, restart_threshold=CRASHLOOP_RESTART_THRESHOLD):
    """Containers stuck in CrashLoopBackOff with enough restarts to matter."""
    if pod.get("kind") != "Pod":
        return []
    findings = []
    for cs in _all_container_statuses(pod):
        waiting = (cs.get("state", {}) or {}).get("waiting") or {}
        restarts = cs.get("restartCount", 0) or 0
        if (
            waiting.get("reason") == "CrashLoopBackOff"
            and restarts >= restart_threshold
        ):
            findings.append(
                {
                    "detector": "crashloop",
                    "severity": "high",
                    "kind": "Pod",
                    "namespace": _namespace(pod),
                    "name": _name(pod),
                    "container": cs.get("name"),
                    "detail": "container %s in CrashLoopBackOff (%d restarts)"
                    % (cs.get("name"), restarts),
                }
            )
    return findings


def detect_oomkilled(pod):
    """Containers killed by the OOM killer (current or previous run)."""
    if pod.get("kind") != "Pod":
        return []
    findings = []
    for cs in _all_container_statuses(pod):
        state = cs.get("state", {}) or {}
        terminated = state.get("terminated") or {}
        last = cs.get("lastState", {}) or {}
        last_terminated = last.get("terminated") or {}
        oom = terminated.get("reason") == "OOMKilled" or last_terminated.get(
            "reason"
        ) == "OOMKilled"
        if oom:
            findings.append(
                {
                    "detector": "oomkilled",
                    "severity": "high",
                    "kind": "Pod",
                    "namespace": _namespace(pod),
                    "name": _name(pod),
                    "container": cs.get("name"),
                    "detail": "container %s was OOMKilled" % cs.get("name"),
                }
            )
    return findings


def detect_failed_job(job):
    """Jobs that failed past their backoff limit."""
    if job.get("kind") != "Job":
        return []
    status = job.get("status", {}) or {}
    spec = job.get("spec", {}) or {}
    failed = status.get("failed", 0) or 0
    backoff_limit = spec.get("backoffLimit", 6)
    conditions = status.get("conditions", []) or []
    marked_failed = any(
        c.get("type") == "Failed" and c.get("status") == "True"
        for c in conditions
    )
    if failed > 0 and (marked_failed or failed >= backoff_limit):
        return [
            {
                "detector": "failed_job",
                "severity": "high",
                "kind": "Job",
                "namespace": _namespace(job),
                "name": _name(job),
                "detail": "job failed %d time(s) (backoffLimit=%s)"
                % (failed, backoff_limit),
            }
        ]
    return []


def detect_pending_pod(pod):
    """Pods stuck Pending because they can't be scheduled."""
    if pod.get("kind") != "Pod":
        return []
    status = pod.get("status", {}) or {}
    if status.get("phase") != "Pending":
        return []
    for cond in status.get("conditions", []) or []:
        if (
            cond.get("type") == "PodScheduled"
            and cond.get("status") == "False"
            and cond.get("reason") == "Unschedulable"
        ):
            return [
                {
                    "detector": "pending",
                    "severity": "medium",
                    "kind": "Pod",
                    "namespace": _namespace(pod),
                    "name": _name(pod),
                    "detail": "pod unschedulable: %s" % cond.get("message", ""),
                }
            ]
    return []


def detect_failed_rollout(deployment):
    """Deployments whose rollout stalled or blew the progress deadline."""
    if deployment.get("kind") != "Deployment":
        return []
    status = deployment.get("status", {}) or {}
    spec = deployment.get("spec", {}) or {}
    desired = spec.get("replicas", 1)
    unavailable = status.get("unavailableReplicas", 0) or 0
    updated = status.get("updatedReplicas", 0) or 0
    deadline_breached = any(
        c.get("type") == "Progressing"
        and c.get("reason") == "ProgressDeadlineExceeded"
        for c in status.get("conditions", []) or []
    )
    stalled = unavailable > 0 and updated < desired
    if deadline_breached or stalled:
        reason = (
            "progress deadline exceeded"
            if deadline_breached
            else "%d/%d replicas updated, %d unavailable"
            % (updated, desired, unavailable)
        )
        return [
            {
                "detector": "failed_rollout",
                "severity": "high",
                "kind": "Deployment",
                "namespace": _namespace(deployment),
                "name": _name(deployment),
                "detail": "rollout failed: %s" % reason,
            }
        ]
    return []


DETECTORS = [
    detect_crashloop,
    detect_oomkilled,
    detect_failed_job,
    detect_pending_pod,
    detect_failed_rollout,
]


def scan(objects):
    """Run every detector over a list of k8s objects. Returns findings."""
    findings = []
    for obj in objects:
        for detector in DETECTORS:
            findings.extend(detector(obj))
    return findings
