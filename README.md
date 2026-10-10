# k8s-self-healer

Half the pages I've ever gotten were for things a script could have fixed: a pod crashlooping for an hour, a deployment stuck mid-rollout, a job that blew its backoff limit at 2 AM and sat there until morning. So I built the script.

k8s-self-healer watches Kubernetes state and applies remediation policies on its own — restart the crashlooping pod, roll back the failed deployment, annotate the dead job for a human to look at. It's deliberately boring automation, not magic. And it's paranoid by design: dry-run is on by default, every decision hits an audit log, and there are circuit breakers so it can't go rogue.

This is a portfolio reference implementation I built from scratch. Not from any employer.

## the honest aiops part

There's also a diagnosis bundler. Point it at a failing workload and it collects everything you'd want before debugging — pod statuses, recent events, detector findings — into one structured JSON document, plus a ready-to-paste prompt for an LLM.

I'm not claiming the tool "does AI". It doesn't. It does the part automation is actually good at: gathering context fast and completely, so you (or a model) can do the reasoning with everything in front of you. That's the only sane division of labor I've seen for incident response, and it's the direction I want to keep pushing.

## how it works

Detectors are pure functions over k8s API objects — no cluster needed to run or test them:

- `crashloop` — containers in CrashLoopBackOff past the restart threshold
- `oomkilled` — containers killed by the OOM killer
- `failed_job` — jobs past their backoff limit
- `pending` — pods stuck unschedulable
- `failed_rollout` — deployments that stalled or blew the progress deadline

Policies live in a YAML file (`examples/policies.yaml`) and map detectors to actions: `delete_pod`, `rollback_deployment`, `scale_deployment`, `cordon_node`, `annotate`. Each policy has its own cooldown, plus namespace allow/deny lists.

Safety rails, because auto-remediation without them is a liability:

- **dry-run by default** — nothing mutates unless you pass `--live`
- **per-policy cooldowns** — won't hammer the same target twice
- **circuit breaker** — max actions per rolling hour, then it stops and says so
- **audit log** — every decision (acted, simulated, or skipped) as JSON lines
- **never silent** — every remediation also annotates the target and emits a Kubernetes Event

## try it (no cluster needed)

```bash
pip install -r requirements.txt

# see what the detectors find in the example snapshot
PYTHONPATH=src python -m self_healer scan --snapshot examples/snapshot.json

# run the policy engine — dry-run, changes nothing
PYTHONPATH=src python -m self_healer run --policies examples/policies.yaml --snapshot examples/snapshot.json

# same, but actually mutate the in-memory fake cluster and write an audit log
PYTHONPATH=src python -m self_healer run --policies examples/policies.yaml --snapshot examples/snapshot.json --live --force --fake --audit-log audit.jsonl

# build an LLM-ready diagnosis bundle for the failing api deployment
PYTHONPATH=src python -m self_healer bundle --kind Deployment --namespace default --name api --snapshot examples/snapshot.json -o bundle.json
```

Against a real cluster (`--cluster`, needs kubeconfig and the `kubernetes` package):

```bash
pip install -r requirements.txt  # uncomment the kubernetes line first
kubectl get pods,deployments,jobs,events -A -o json > snapshot.json
PYTHONPATH=src python -m self_healer run --policies examples/policies.yaml --snapshot snapshot.json --live --cluster
```

Note: `--live` also refuses unless the policy file sets `global.dry_run: false` or you pass `--force`. Two separate "are you sure"s before anything mutates. That's intentional.

Or Docker:

```bash
docker build -t self-healer .
docker run --rm -v $(pwd)/examples:/data self-healer scan --snapshot /data/snapshot.json
```

## repo layout

- `src/self_healer/detectors.py` — pure-function detectors, one per failure mode
- `src/self_healer/policies.py` — YAML policy loading and validation
- `src/self_healer/remediations.py` — the engine: matching, cooldowns, circuit breaker, audit
- `src/self_healer/bundler.py` — diagnosis bundle + LLM prompt builder
- `src/self_healer/clients.py` — cluster interface; dry-run, in-memory fake, and real (optional) clients
- `src/self_healer/audit.py` — JSON-lines audit log
- `src/self_healer/cli.py` — `scan` / `run` / `bundle` commands
- `tests/` — 39 pytest tests, no cluster required
- `examples/` — sample policies and a snapshot with one of each failure

## dev

```bash
make test    # pytest, must be green
make lint    # py_compile + yaml syntax on everything
make smoke   # scan + dry-run + bundle against the examples
```

## license

MIT.
