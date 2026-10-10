"""CLI: self-healer scan | run | bundle

    self-healer scan --policies examples/policies.yaml --snapshot examples/snapshot.json
    self-healer run  --policies examples/policies.yaml --snapshot examples/snapshot.json [--live] [--audit-log audit.jsonl]
    self-healer bundle --kind Deployment --namespace default --name api --snapshot examples/snapshot.json [-o bundle.json]

--live is required for real mutations. Without it everything is dry-run.
"""

import argparse
import json
import sys

from self_healer import detectors
from self_healer import policies as policy_mod
from self_healer import remediations
from self_healer import bundler
from self_healer.audit import AuditLog
from self_healer.clients import DryRunClient, FakeClient, make_real_client


def load_snapshot(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        # kubectl -o json gives {"kind": "List", "items": [...]}
        if data.get("kind") == "List" and "items" in data:
            return data["items"]
        objects = []
        for key in ("pods", "deployments", "jobs", "events", "nodes"):
            objects.extend(data.get(key, []) or [])
        objects.extend(data.get("objects", []) or [])
        return objects
    return data


def cmd_scan(args):
    objects = load_snapshot(args.snapshot)
    findings = detectors.scan(objects)
    for f in findings:
        print(
            "[%s] %s/%s %s: %s"
            % (
                f["severity"].upper(),
                f["namespace"],
                f["name"],
                f["detector"],
                f["detail"],
            )
        )
    print("%d finding(s)" % len(findings))
    return 0


def cmd_run(args):
    global_cfg, policy_list = policy_mod.load(args.policies)
    objects = load_snapshot(args.snapshot)
    findings = detectors.scan(objects)

    dry_run = not args.live
    if args.live and global_cfg.get("dry_run", True) and not args.force:
        print(
            "refusing: policies.yaml sets global.dry_run=true. "
            "Pass --force to override, or set global.dry_run=false."
        )
        return 2

    if args.cluster:
        client = make_real_client()
    elif args.fake:
        client = FakeClient(dry_run=dry_run)
    else:
        client = DryRunClient()

    safety = remediations.SafetyState(
        max_actions_per_hour=global_cfg["max_actions_per_hour"]
    )
    audit = AuditLog(args.audit_log)
    try:
        decisions = remediations.remediate(
            findings, policy_list, client, safety, audit, dry_run=dry_run
        )
    finally:
        audit.close()

    acted = sum(1 for d in decisions if d["outcome"] == "acted")
    simulated = sum(1 for d in decisions if d["outcome"] == "dry_run")
    skipped = len(decisions) - acted - simulated
    mode = "LIVE" if not dry_run else "DRY-RUN"
    print(
        "%s: %d finding(s), %d action(s) %s, %d skipped"
        % (
            mode,
            len(findings),
            acted if not dry_run else simulated,
            "taken" if not dry_run else "simulated",
            skipped,
        )
    )
    if args.audit_log:
        print("audit log: %s" % args.audit_log)
    return 0


def cmd_bundle(args):
    objects = load_snapshot(args.snapshot)
    workload = None
    for obj in objects:
        meta = obj.get("metadata", {}) or {}
        if (
            obj.get("kind") == args.kind
            and meta.get("name") == args.name
            and meta.get("namespace", "default") == args.namespace
        ):
            workload = obj
            break
    if workload is None:
        print(
            "workload %s/%s (%s) not found in snapshot"
            % (args.namespace, args.name, args.kind),
            file=sys.stderr,
        )
        return 1
    bundle = bundler.build_bundle(workload, objects)
    out = bundler.bundle_to_json(bundle)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        print("bundle written to %s" % args.output)
    else:
        print(out)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="self-healer",
        description="Policy-driven autonomous remediation for Kubernetes. "
        "Dry-run by default; --live actually mutates.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="run detectors over a snapshot, print findings")
    s.add_argument("--snapshot", required=True, help="JSON snapshot file")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("run", help="evaluate policies and remediate findings")
    r.add_argument("--policies", required=True, help="policy YAML file")
    r.add_argument("--snapshot", required=True, help="JSON snapshot file")
    r.add_argument("--live", action="store_true", help="actually mutate the cluster")
    r.add_argument("--force", action="store_true", help="override global.dry_run=true")
    r.add_argument("--cluster", action="store_true", help="use real cluster client (needs kubeconfig)")
    r.add_argument("--fake", action="store_true", help="use in-memory fake client")
    r.add_argument("--audit-log", default=None, help="JSON-lines audit log path")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("bundle", help="build an LLM-ready diagnosis bundle for a workload")
    b.add_argument("--kind", default="Deployment")
    b.add_argument("--namespace", default="default")
    b.add_argument("--name", required=True)
    b.add_argument("--snapshot", required=True, help="JSON snapshot file")
    b.add_argument("-o", "--output", default=None)
    b.set_defaults(func=cmd_bundle)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
