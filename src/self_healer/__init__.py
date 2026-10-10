"""k8s-self-healer — policy-driven autonomous remediation for Kubernetes.

Watches cluster state, detects common failure modes with pure functions,
and applies remediation policies. Dry-run is ON by default: nothing
mutates unless --live is passed. Every action is written to an audit log.
"""

__version__ = "0.1.0"
