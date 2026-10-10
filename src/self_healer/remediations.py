"""The remediation engine: findings in, audited actions out.

For each finding, it looks up matching policies, then applies the
safety rails before touching anything:

1. Namespace allowlist/denylist (from the policy).
2. Per-policy cooldown: don't act on the same target twice within the
   cooldown window.
3. Circuit breaker: no more than max_actions_per_hour in a rolling hour.
   When tripped, everything stops and it's recorded in the audit log.

Every remediation is paired with two non-optional side effects:
annotate the target and emit a Kubernetes Event, so the healer never
acts silently. In dry-run mode the annotate/event calls go through the
DryRunClient like everything else — recorded, not sent.
"""

import time


class SafetyState:
    """Tracks cooldowns and the rolling action window."""

    def __init__(self, max_actions_per_hour=20, clock=None):
        self.max_actions_per_hour = max_actions_per_hour
        self.clock = clock or time.time
        self.last_action = {}  # (policy_name, target_key) -> epoch seconds
        self.action_times = []  # epoch seconds of taken actions

    def cooldown_ok(self, policy_name, target_key, cooldown_minutes):
        last = self.last_action.get((policy_name, target_key))
        if last is None:
            return True
        return (self.clock() - last) >= cooldown_minutes * 60

    def breaker_ok(self):
        now = self.clock()
        self.action_times = [t for t in self.action_times if now - t < 3600]
        return len(self.action_times) < self.max_actions_per_hour

    def note_action(self, policy_name, target_key):
        now = self.clock()
        self.last_action[(policy_name, target_key)] = now
        self.action_times.append(now)


def _target_key(finding):
    return (
        finding.get("kind"),
        finding.get("namespace", "default"),
        finding.get("name"),
    )


HEALER_ANNOTATION = "self-healer.io/last-action"


def apply_action(policy, finding, client, dry_run=True):
    """Execute one policy's action against the finding's target.

    Returns a result dict describing what happened (or would happen).
    """
    action = policy["action"]
    action_type = action["type"]
    namespace = finding.get("namespace", "default")
    name = finding.get("name")
    kind = finding.get("kind")

    if action_type == "delete_pod":
        result = client.delete_pod(namespace, name)
    elif action_type == "rollback_deployment":
        result = client.rollback_deployment(namespace, name)
    elif action_type == "scale_deployment":
        result = client.scale_deployment(
            namespace, name, action["replicas"]
        )
    elif action_type == "cordon_node":
        node = finding.get("node") or name
        result = client.cordon_node(node)
    elif action_type == "annotate":
        result = client.annotate(
            namespace,
            kind,
            name,
            {HEALER_ANNOTATION: policy["name"]},
        )
    else:  # pragma: no cover — validated at policy load
        raise ValueError("unknown action type %r" % action_type)

    # Never act silently: annotate the target and emit an Event.
    client.annotate(
        namespace, kind, name, {HEALER_ANNOTATION: policy["name"]}
    )
    client.emit_event(
        namespace,
        name,
        kind,
        reason="SelfHealerRemediation",
        message="self-healer policy %r applied action %r (%s)"
        % (policy["name"], action_type, "dry-run" if dry_run else "live"),
    )
    return result


def remediate(findings, policies, client, safety, audit, dry_run=True):
    """Run the engine over findings. Returns a list of decision dicts.

    Each decision is recorded in the audit log with outcome "acted",
    "dry_run", "skipped_cooldown", "skipped_breaker", or
    "no_matching_policy".
    """
    from self_healer import policies as policy_mod

    decisions = []
    for finding in findings:
        matched = policy_mod.matching_policies(finding, policies)
        if not matched:
            decision = {
                "outcome": "no_matching_policy",
                "finding": finding,
            }
            audit.record(**decision)
            decisions.append(decision)
            continue
        for policy in matched:
            target = _target_key(finding)
            if not safety.cooldown_ok(
                policy["name"], target, policy["cooldown_minutes"]
            ):
                decision = {
                    "outcome": "skipped_cooldown",
                    "policy": policy["name"],
                    "finding": finding,
                }
                audit.record(**decision)
                decisions.append(decision)
                continue
            if not safety.breaker_ok():
                decision = {
                    "outcome": "skipped_breaker",
                    "policy": policy["name"],
                    "finding": finding,
                }
                audit.record(**decision)
                decisions.append(decision)
                continue
            result = apply_action(policy, finding, client, dry_run=dry_run)
            safety.note_action(policy["name"], target)
            decision = {
                "outcome": "dry_run" if dry_run else "acted",
                "policy": policy["name"],
                "finding": finding,
                "action": policy["action"]["type"],
                "result": result,
            }
            audit.record(**decision)
            decisions.append(decision)
    return decisions
