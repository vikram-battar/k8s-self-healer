"""Policy config: YAML file mapping detectors to remediation actions.

Example:

    global:
      dry_run: true            # nothing mutates unless --live is passed
      max_actions_per_hour: 20
      default_cooldown_minutes: 15

    policies:
      - name: restart-crashloop-pods
        match:
          detector: crashloop
        action:
          type: delete_pod     # pod gets recreated by its controller
        cooldown_minutes: 30
        namespaces: [default, staging]
        exclude_namespaces: [kube-system]

      - name: rollback-failed-deployments
        match:
          detector: failed_rollout
        action:
          type: rollback_deployment
        cooldown_minutes: 60

Supported action types: delete_pod, rollback_deployment, scale_deployment,
cordon_node, annotate. scale_deployment needs action.replicas.
"""

import yaml

ACTION_TYPES = {
    "delete_pod",
    "rollback_deployment",
    "scale_deployment",
    "cordon_node",
    "annotate",
}


def load(path):
    """Load and validate a policy file. Returns (global_cfg, policies)."""
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return parse(cfg)


def parse(cfg):
    global_cfg = cfg.get("global", {}) or {}
    global_cfg.setdefault("dry_run", True)
    global_cfg.setdefault("max_actions_per_hour", 20)
    global_cfg.setdefault("default_cooldown_minutes", 15)

    policies = []
    for raw in cfg.get("policies", []) or []:
        policy = validate_policy(raw, global_cfg)
        policies.append(policy)
    return global_cfg, policies


def validate_policy(raw, global_cfg):
    if not raw.get("name"):
        raise ValueError("policy is missing 'name': %r" % (raw,))
    match = raw.get("match", {}) or {}
    if "detector" not in match:
        raise ValueError("policy %r: match.detector is required" % raw["name"])
    action = raw.get("action", {}) or {}
    action_type = action.get("type")
    if action_type not in ACTION_TYPES:
        raise ValueError(
            "policy %r: unknown action type %r (want one of %s)"
            % (raw["name"], action_type, sorted(ACTION_TYPES))
        )
    if action_type == "scale_deployment" and "replicas" not in action:
        raise ValueError(
            "policy %r: scale_deployment needs action.replicas"
            % raw["name"]
        )
    return {
        "name": raw["name"],
        "match": match,
        "action": action,
        "cooldown_minutes": raw.get(
            "cooldown_minutes", global_cfg["default_cooldown_minutes"]
        ),
        "namespaces": raw.get("namespaces"),  # None = all allowed
        "exclude_namespaces": raw.get("exclude_namespaces") or [],
    }


def namespace_allowed(policy, namespace):
    if namespace in policy["exclude_namespaces"]:
        return False
    allowed = policy["namespaces"]
    if allowed is not None and namespace not in allowed:
        return False
    return True


def matching_policies(finding, policies):
    """Policies whose match.detector equals the finding's detector and
    whose namespace rules allow the finding's namespace."""
    out = []
    for policy in policies:
        if policy["match"]["detector"] != finding.get("detector"):
            continue
        if not namespace_allowed(policy, finding.get("namespace", "default")):
            continue
        out.append(policy)
    return out
