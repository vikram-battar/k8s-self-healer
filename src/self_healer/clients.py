"""Cluster clients behind one small interface.

The remediation engine only talks to this interface, so everything is
testable without a cluster:

- DryRunClient: records intended calls, mutates nothing (default).
- FakeClient: in-memory fake that actually applies the mutations, for tests.
- RealClient: thin wrapper over the official kubernetes python client.
  Only constructed when the `kubernetes` package is installed; the rest
  of this project never imports it.
"""


class K8sClient:
    """Interface the engine needs. Subclass or duck-type it."""

    def delete_pod(self, namespace, name):
        raise NotImplementedError

    def rollback_deployment(self, namespace, name):
        raise NotImplementedError

    def scale_deployment(self, namespace, name, replicas):
        raise NotImplementedError

    def cordon_node(self, name):
        raise NotImplementedError

    def annotate(self, namespace, kind, name, annotations):
        raise NotImplementedError

    def emit_event(self, namespace, name, kind, reason, message):
        raise NotImplementedError


class DryRunClient(K8sClient):
    """Records every call, changes nothing. This is the default."""

    def __init__(self):
        self.calls = []

    def _record(self, method, **kwargs):
        self.calls.append({"method": method, "kwargs": kwargs})
        return {"dry_run": True, "method": method, "kwargs": kwargs}

    def delete_pod(self, namespace, name):
        return self._record("delete_pod", namespace=namespace, name=name)

    def rollback_deployment(self, namespace, name):
        return self._record(
            "rollback_deployment", namespace=namespace, name=name
        )

    def scale_deployment(self, namespace, name, replicas):
        return self._record(
            "scale_deployment",
            namespace=namespace,
            name=name,
            replicas=replicas,
        )

    def cordon_node(self, name):
        return self._record("cordon_node", name=name)

    def annotate(self, namespace, kind, name, annotations):
        return self._record(
            "annotate",
            namespace=namespace,
            kind=kind,
            name=name,
            annotations=annotations,
        )

    def emit_event(self, namespace, name, kind, reason, message):
        return self._record(
            "emit_event",
            namespace=namespace,
            name=name,
            kind=kind,
            reason=reason,
            message=message,
        )


class FakeClient(DryRunClient):
    """In-memory fake that applies mutations to a dict of objects.

    objects: {(kind, namespace, name): object_dict}
    When dry_run=True it records calls but mutates nothing.
    """

    def __init__(self, objects=None, dry_run=False):
        super().__init__()
        self.objects = dict(objects or {})
        self.cordoned_nodes = set()
        self.events = []
        self.dry_run = dry_run

    def delete_pod(self, namespace, name):
        self._record("delete_pod", namespace=namespace, name=name)
        if self.dry_run:
            return {"dry_run": True}
        key = ("Pod", namespace, name)
        existed = self.objects.pop(key, None) is not None
        return {"deleted": existed}

    def rollback_deployment(self, namespace, name):
        self._record("rollback_deployment", namespace=namespace, name=name)
        if self.dry_run:
            return {"dry_run": True}
        dep = self.objects.get(("Deployment", namespace, name))
        if dep is None:
            return {"rolled_back": False, "reason": "deployment not found"}
        # Simulate rollback: mark the previous revision as current.
        anns = dep.setdefault("metadata", {}).setdefault("annotations", {})
        anns["self-healer.io/rolled-back"] = "true"
        return {"rolled_back": True}

    def scale_deployment(self, namespace, name, replicas):
        self._record(
            "scale_deployment",
            namespace=namespace,
            name=name,
            replicas=replicas,
        )
        if self.dry_run:
            return {"dry_run": True}
        dep = self.objects.get(("Deployment", namespace, name))
        if dep is None:
            return {"scaled": False, "reason": "deployment not found"}
        dep.setdefault("spec", {})["replicas"] = replicas
        return {"scaled": True, "replicas": replicas}

    def cordon_node(self, name):
        self._record("cordon_node", name=name)
        if self.dry_run:
            return {"dry_run": True}
        self.cordoned_nodes.add(name)
        return {"cordoned": True}

    def annotate(self, namespace, kind, name, annotations):
        self._record(
            "annotate",
            namespace=namespace,
            kind=kind,
            name=name,
            annotations=annotations,
        )
        if self.dry_run:
            return {"dry_run": True}
        obj = self.objects.get((kind, namespace, name))
        if obj is None:
            return {"annotated": False, "reason": "object not found"}
        obj.setdefault("metadata", {}).setdefault("annotations", {}).update(
            annotations
        )
        return {"annotated": True}

    def emit_event(self, namespace, name, kind, reason, message):
        self._record(
            "emit_event",
            namespace=namespace,
            name=name,
            kind=kind,
            reason=reason,
            message=message,
        )
        if self.dry_run:
            return {"dry_run": True}
        self.events.append(
            {
                "namespace": namespace,
                "involved_object": {"kind": kind, "name": name},
                "reason": reason,
                "message": message,
            }
        )
        return {"emitted": True}


def make_real_client():
    """Build a RealClient from kubeconfig / in-cluster config.

    Raises ImportError if the kubernetes package isn't installed.
    """
    from kubernetes import client, config  # noqa: F401  (optional dep)

    try:
        config.load_kube_config()
    except Exception:
        config.load_incluster_config()

    from kubernetes import client as k8s_client

    core = k8s_client.CoreV1Api()
    apps = k8s_client.AppsV1Api()

    class RealClient(K8sClient):
        def delete_pod(self, namespace, name):
            core.delete_namespaced_pod(name, namespace)
            return {"deleted": True}

        def rollback_deployment(self, namespace, name):
            # Roll back by re-applying the previous ReplicaSet template.
            # Kept simple: patch the deployment's pod template from the
            # newest non-current ReplicaSet owned by it.
            rss = apps.list_namespaced_replica_set(namespace).items
            mine = [
                rs
                for rs in rss
                if any(
                    o.get("kind") == "Deployment" and o.get("name") == name
                    for o in [
                        {"kind": ref.kind, "name": ref.name}
                        for ref in (rs.metadata.owner_references or [])
                    ]
                )
            ]
            mine.sort(
                key=lambda rs: rs.metadata.annotations.get(
                    "deployment.kubernetes.io/revision", "0"
                )
            )
            if len(mine) < 2:
                return {"rolled_back": False, "reason": "no prior revision"}
            prev = mine[-2]
            apps.patch_namespaced_deployment(
                name,
                namespace,
                {"spec": {"template": prev.spec.template}},
            )
            return {"rolled_back": True}

        def scale_deployment(self, namespace, name, replicas):
            apps.patch_namespaced_deployment_scale(
                name, namespace, {"spec": {"replicas": replicas}}
            )
            return {"scaled": True, "replicas": replicas}

        def cordon_node(self, name):
            core.patch_node(name, {"spec": {"unschedulable": True}})
            return {"cordoned": True}

        def annotate(self, namespace, kind, name, annotations):
            body = {"metadata": {"annotations": annotations}}
            if kind == "Pod":
                core.patch_namespaced_pod(name, namespace, body)
            elif kind == "Deployment":
                apps.patch_namespaced_deployment(name, namespace, body)
            else:
                raise ValueError("annotate: unsupported kind %r" % kind)
            return {"annotated": True}

        def emit_event(self, namespace, name, kind, reason, message):
            import datetime

            event = k8s_client.CoreV1Event(
                metadata=k8s_client.V1ObjectMeta(
                    generate_name="self-healer-", namespace=namespace
                ),
                involved_object=k8s_client.V1ObjectReference(
                    kind=kind, name=name, namespace=namespace
                ),
                reason=reason,
                message=message,
                type="Warning",
                first_timestamp=datetime.datetime.now(datetime.timezone.utc),
                last_timestamp=datetime.datetime.now(datetime.timezone.utc),
                count=1,
                source=k8s_client.V1EventSource(component="k8s-self-healer"),
            )
            core.create_namespaced_event(namespace, event)
            return {"emitted": True}

    return RealClient()
