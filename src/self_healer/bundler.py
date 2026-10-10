"""Incident-context bundler: the honest AIOps bridge.

This module does NOT diagnose anything. It collects everything an
on-call engineer (or an LLM) needs to diagnose a failing workload into
one structured JSON document: the workload spec, its pods' container
statuses, recent related events, and the detector findings.

The bundle includes a ready-to-paste `llm_prompt` field: the context
formatted as a prompt you can hand to any LLM for incident diagnosis.
The tool prepares the context; the model does the reasoning. That's the
whole point — automation where it's reliable, AI where judgment lives.
"""

import json

from self_healer.audit import utcnow_iso


def _related_pods(workload, pods):
    """Pods owned by the workload (by name prefix or label match)."""
    kind = workload.get("kind")
    meta = workload.get("metadata", {}) or {}
    name = meta.get("name", "")
    namespace = meta.get("namespace", "default")
    labels = meta.get("labels", {}) or {}
    app = labels.get("app")

    related = []
    for pod in pods:
        if pod.get("kind") != "Pod":
            continue
        pmeta = pod.get("metadata", {}) or {}
        if pmeta.get("namespace", "default") != namespace:
            continue
        pname = pmeta.get("name", "")
        plabels = pmeta.get("labels", {}) or {}
        if pname.startswith(name + "-") or (
            app and plabels.get("app") == app
        ):
            related.append(pod)
    return related


def _pod_summary(pod):
    pmeta = pod.get("metadata", {}) or {}
    pstatus = pod.get("status", {}) or {}
    containers = []
    for cs in (pstatus.get("containerStatuses", []) or []) + (
        pstatus.get("initContainerStatuses", []) or []
    ):
        state = cs.get("state", {}) or {}
        if "waiting" in state:
            st = "waiting: %s" % (state["waiting"] or {}).get("reason")
        elif "terminated" in state:
            t = state["terminated"] or {}
            st = "terminated: %s (exit %s)" % (
                t.get("reason"),
                t.get("exitCode"),
            )
        else:
            st = "running"
        containers.append(
            {
                "name": cs.get("name"),
                "state": st,
                "restarts": cs.get("restartCount", 0),
                "ready": cs.get("ready"),
            }
        )
    return {
        "name": pmeta.get("name"),
        "phase": pstatus.get("phase"),
        "containers": containers,
    }


def _related_events(workload, pods, events, limit=25):
    """Events mentioning the workload or its pods, newest first."""
    names = {workload.get("metadata", {}).get("name")}
    names.update(p.get("metadata", {}).get("name") for p in pods)
    related = []
    for ev in events:
        involved = ev.get("involvedObject", {}) or {}
        if involved.get("name") in names:
            related.append(
                {
                    "reason": ev.get("reason"),
                    "message": ev.get("message"),
                    "type": ev.get("type"),
                    "count": ev.get("count"),
                    "last_seen": ev.get("lastTimestamp"),
                }
            )
    return related[:limit]


def build_prompt(bundle):
    """Render the bundle as a paste-ready LLM diagnosis prompt."""
    w = bundle["workload"]
    lines = [
        "You are an SRE helping diagnose a Kubernetes incident.",
        "Here is the collected context. Identify the most likely root cause,",
        "explain your reasoning briefly, and suggest the next concrete step.",
        "",
        "WORKLOAD: %s/%s (%s)" % (w["namespace"], w["name"], w["kind"]),
        "",
        "FINDINGS:",
    ]
    for f in bundle["findings"]:
        lines.append(
            "- [%s] %s: %s" % (f["severity"], f["detector"], f["detail"])
        )
    lines.append("")
    lines.append("PODS:")
    for p in bundle["pods"]:
        lines.append("- %s (%s)" % (p["name"], p["phase"]))
        for c in p["containers"]:
            lines.append(
                "  - %s: %s, restarts=%s"
                % (c["name"], c["state"], c["restarts"])
            )
    lines.append("")
    lines.append("RECENT EVENTS:")
    for e in bundle["recent_events"]:
        lines.append(
            "- [%s] %s (x%s): %s"
            % (e["type"], e["reason"], e["count"], e["message"])
        )
    return "\n".join(lines)


def build_bundle(workload, objects, findings=None):
    """Assemble a diagnosis bundle for one workload.

    workload: the Deployment/Job/StatefulSet/... dict.
    objects: full snapshot list (pods, events, ...).
    findings: detector findings (computed if omitted).
    """
    from self_healer import detectors as det

    pods = [o for o in objects if o.get("kind") == "Pod"]
    events = [o for o in objects if o.get("kind") == "Event"]
    related_pods = _related_pods(workload, pods)
    if findings is None:
        findings = det.scan([workload] + related_pods)

    wmeta = workload.get("metadata", {}) or {}
    bundle = {
        "schema": "self-healer/diagnosis-bundle/v1",
        "collected_at": utcnow_iso(),
        "workload": {
            "kind": workload.get("kind"),
            "namespace": wmeta.get("namespace", "default"),
            "name": wmeta.get("name"),
            "labels": wmeta.get("labels", {}) or {},
        },
        "findings": findings,
        "pods": [_pod_summary(p) for p in related_pods],
        "recent_events": _related_events(workload, related_pods, events),
    }
    bundle["llm_prompt"] = build_prompt(bundle)
    return bundle


def bundle_to_json(bundle):
    return json.dumps(bundle, indent=2)
