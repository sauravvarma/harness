"""harness: the deterministic spine of the orchestration harness.

Tag: kernel. This CLI is the sole writer of the ledger. Models (orchestrator
and delegatees) propose actions; this machine validates and disposes. Illegal
state transitions are hard errors even when the orchestrator requests them.

Design doc: ~/vault/projects/orchestrator-harness/design.md (v1.3).
All internal seams are UNSTABLE until the falsifiability clause fires.
"""

import argparse
import datetime
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

HARNESS_DIR = ".harness"
MODES = ("standard", "charter", "genesis")

# Gate floor is compiled in and non-removable (v1.3 amendment 5).
GATE_FLOOR = ("digest-valid", "acceptance-ran", "critic", "verifier")
# Machine-owned gates: recorded by this CLI, never by `harness gate`.
MACHINE_GATES = ("digest-valid", "acceptance-ran")

DIGEST_STATUSES = (
    "done",
    "needs-decision",
    "contract-conflict",
    "scope-discovery",
    "budget-exceeded",
)

USAGE_ROLES = (
    "orchestrator",
    "builder",
    "critic",
    "verifier",
    "design-critic",
    "integrator",
    "cartographer",
    "plan-review",
    "other",
)

HYPOTHESIS_SCHEMES = (
    "solo-frontier",
    "frontier-builders",
    "sonnet-builders",
    "single-session",
    "other",
)
HYPOTHESIS_VERDICTS = ("likely-better", "unclear", "likely-worse")

RISK_LEVELS = ("low", "standard", "high")

# Orchestrator model guard: frontier ids contain one of these substrings.
FRONTIER_MARKERS = ("fable", "opus")

STATES = (
    "planned",
    "dispatched",
    "escalated",
    "in_gate",
    "done",
    "integrated",
    "deployed",
)

TRANSITIONS = {
    ("planned", "dispatched"),
    ("dispatched", "in_gate"),
    ("dispatched", "escalated"),
    ("escalated", "dispatched"),
    ("in_gate", "dispatched"),
    ("in_gate", "done"),
    ("done", "dispatched"),
    ("done", "integrated"),
    ("integrated", "deployed"),
}

ACCEPTANCE_TIMEOUT = 600


class HarnessError(Exception):
    pass


# ---------------------------------------------------------------- plumbing


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def ts_epoch(ts):
    """Parse a ledger timestamp back to epoch seconds."""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(ts, fmt).timestamp()
        except ValueError:
            continue
    raise HarnessError("unparseable ledger timestamp: %r" % ts)


def find_root(start=None):
    p = Path(start or os.getcwd()).resolve()
    for candidate in [p] + list(p.parents):
        if (candidate / HARNESS_DIR).is_dir():
            return candidate
    raise HarnessError("no %s found here or above; run `harness init` first" % HARNESS_DIR)


def hdir(root):
    return root / HARNESS_DIR


def load_json(path, what):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise HarnessError("%s missing at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise HarnessError("%s is not valid JSON (%s)" % (what, e))


def session(root):
    return load_json(hdir(root) / "session.json", "session header")


def manifest(root):
    return load_json(hdir(root) / "manifest.json", "manifest")


def save_manifest(root, m):
    with open(hdir(root) / "manifest.json", "w") as f:
        json.dump(m, f, indent=2)


def read_events(root):
    path = hdir(root) / "ledger.jsonl"
    events = []
    if path.exists():
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    return events


def append_event(root, ev):
    """Single writer, append only. Every event inherits the session header."""
    s = session(root)
    events_path = hdir(root) / "ledger.jsonl"
    seq = 0
    if events_path.exists():
        with open(events_path) as f:
            seq = sum(1 for line in f if line.strip())
    record = {"seq": seq, "ts": now(), "session": s["session_id"], "operator": s["operator"]}
    record.update(ev)
    with open(events_path, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def git_sha(cwd):
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(cwd), capture_output=True, text=True
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except OSError:
        pass
    return "none"


# ---------------------------------------------------------------- derivation


def task_defs(events):
    defs = {}
    for e in events:
        if e["ev"] == "PLANNED":
            defs[e["task"]] = e
    return defs


def derive_states(events):
    st = {}
    for e in events:
        if e["ev"] == "PLANNED":
            st[e["task"]] = "planned"
        elif e["ev"] == "STATE":
            st[e["task"]] = e["state"]
    return st


def attempts_of(events, task):
    return sum(1 for e in events if e["ev"] == "DISPATCHED" and e["task"] == task)


def set_state(root, events, task, new_state):
    """The only path to a state change. Validates the transition."""
    current = derive_states(events).get(task)
    if current is None:
        raise HarnessError("task %s is not planned" % task)
    if (current, new_state) not in TRANSITIONS:
        raise HarnessError(
            "illegal transition for %s: %s -> %s" % (task, current, new_state)
        )
    return append_event(root, {"ev": "STATE", "task": task, "state": new_state})


def deps_satisfied(events, states, tdef):
    gate = tdef.get("dep_gate", "integrated")
    ok_states = ("integrated", "deployed") if gate == "integrated" else ("done", "integrated", "deployed")
    return all(states.get(dep) in ok_states for dep in tdef.get("deps", []))


def ready_set(events):
    states = derive_states(events)
    defs = task_defs(events)
    out = []
    for tid, tdef in defs.items():
        if states.get(tid) == "planned" and deps_satisfied(events, states, tdef):
            out.append(tid)
    return sorted(out)


def required_gates(tdef):
    return list(GATE_FLOOR) + list(tdef.get("extra_gates", []))


def gate_results(events, task, attempt):
    """Latest result per gate for the given attempt."""
    latest = {}
    for e in events:
        if e["ev"] == "GATE_RESULT" and e["task"] == task and e.get("attempt") == attempt:
            latest[e["gate"]] = e["result"]
    return latest


def latest_escalation_seq(events, task):
    seq = None
    for e in events:
        if e["ev"] == "DIGEST" and e["task"] == task and e.get("status") != "done":
            seq = e["seq"]
    return seq


def latest_done_digest(events, task):
    """Returns (seq, digest dict) of the most recent accepted done digest."""
    found = (None, None)
    for e in events:
        if e["ev"] == "DIGEST" and e["task"] == task and e.get("status") == "done":
            found = (e["seq"], e.get("digest", {}))
    return found


def decision_after(events, task, seq):
    return any(
        e["ev"] == "DECISION" and e.get("task") == task and e["seq"] > seq for e in events
    )


def open_park(events):
    """The latest PARKED event with no later UNPARKED, or None."""
    latest = None
    for e in events:
        if e["ev"] == "PARKED":
            latest = e
        elif e["ev"] == "UNPARKED":
            latest = None
    return latest


# ---------------------------------------------------------------- commands


def cmd_init(args):
    root = Path(os.getcwd())
    hd = root / HARNESS_DIR
    if hd.exists():
        lease_path = hd / "lease.json"
        if lease_path.exists():
            lease = load_json(lease_path, "lease")
            raise HarnessError(
                "repo lease held by %s (host %s, since %s); run `harness release` if that run is over"
                % (lease.get("operator"), lease.get("host"), lease.get("since"))
            )
        raise HarnessError("%s already exists; use `harness status` to resume" % HARNESS_DIR)
    if args.mode not in MODES:
        raise HarnessError("mode must be one of %s" % (MODES,))

    model = args.orchestrator_model
    non_frontier_ack = False
    if model:
        if not any(m in model.lower() for m in FRONTIER_MARKERS):
            if not args.allow_non_frontier:
                raise HarnessError(
                    "orchestrator model %s is not frontier (expected an id containing %s); switch model or pass --allow-non-frontier"
                    % (model, " or ".join(FRONTIER_MARKERS))
                )
            non_frontier_ack = True

    for sub in ("packets", "reports", "digests", "corpus"):
        (hd / sub).mkdir(parents=True, exist_ok=True)

    operator = args.operator or os.environ.get("USER", "unknown")
    harness_sha = git_sha(Path(__file__).resolve().parent.parent)
    header = {
        "session_id": str(uuid.uuid4()),
        "operator": operator,
        "repo": str(root),
        "task_type": args.task_type,
        "mode": args.mode,
        "host": socket.gethostname(),
        "harness_sha": harness_sha,
        "surface_versions": {"harness": harness_sha},
        "orchestrator_model": model or "",
        "non_frontier_ack": non_frontier_ack,
        "started": now(),
    }
    with open(hd / "session.json", "w") as f:
        json.dump(header, f, indent=2)
    with open(hd / "lease.json", "w") as f:
        json.dump({"operator": operator, "host": socket.gethostname(), "pid": os.getpid(), "since": now()}, f, indent=2)
    with open(hd / "manifest.json", "w") as f:
        json.dump({"metrics": {}}, f, indent=2)

    append_event(root, {"ev": "INIT", "mode": args.mode, "base_sha": git_sha(root),
                        "task_type": args.task_type, "orchestrator_model": model or "",
                        "non_frontier_ack": non_frontier_ack})
    print("initialized %s (mode=%s, operator=%s)" % (hd, args.mode, operator))


def cmd_release(args):
    root = find_root()
    lease = hdir(root) / "lease.json"
    if lease.exists():
        lease.unlink()
        append_event(root, {"ev": "LEASE_RELEASED"})
        print("lease released")
    else:
        print("no lease held")


def _detect_cycle(graph):
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    def visit(n, stack):
        color[n] = GRAY
        for dep in graph.get(n, []):
            if color.get(dep) == GRAY:
                raise HarnessError("dependency cycle involving %s" % " -> ".join(stack + [dep]))
            if color.get(dep) == WHITE:
                visit(dep, stack + [dep])
        color[n] = BLACK
    for n in list(graph):
        if color[n] == WHITE:
            visit(n, [n])


def cmd_plan(args):
    root = find_root()
    events = read_events(root)
    plan = load_json(Path(args.file), "plan file")

    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise HarnessError("plan must contain a non-empty tasks list")

    defaults = plan.get("defaults", {})
    if not isinstance(defaults, dict):
        raise HarnessError("plan defaults must be an object")
    default_builder = defaults.get("builder_model", "")
    if not isinstance(default_builder, str):
        raise HarnessError("defaults.builder_model must be a string")

    existing = task_defs(events)
    known_contracts = set()
    for e in events:
        if e["ev"] in ("CONTRACT_DEFINED", "CONTRACT_AMENDED"):
            known_contracts.add(e["contract"])
    new_contracts = plan.get("contracts", {})

    new_ids = []
    for t in tasks:
        tid = t.get("id")
        if not tid or not isinstance(tid, str):
            raise HarnessError("every task needs a string id")
        if tid in existing or tid in new_ids:
            raise HarnessError("task id %s already planned; plan revisions may only add new tasks" % tid)
        new_ids.append(tid)
        if not t.get("goal"):
            raise HarnessError("task %s needs a goal" % tid)
        acc = t.get("acceptance")
        if not isinstance(acc, list) or not acc or not all(isinstance(c, str) and c.strip() for c in acc):
            raise HarnessError("task %s needs a non-empty list of executable acceptance commands" % tid)
        if t.get("dep_gate", "integrated") not in ("integrated", "done"):
            raise HarnessError("task %s dep_gate must be integrated or done" % tid)
        for g in t.get("extra_gates", []):
            if g in GATE_FLOOR:
                raise HarnessError("task %s: %s is a floor gate, not an extra gate" % (tid, g))
        if t.get("direction_adjacent") and not t.get("ui_surface"):
            raise HarnessError("task %s: direction_adjacent implies ui_surface" % tid)
        if t.get("ui_surface") and "design-critic" not in t.get("extra_gates", []):
            raise HarnessError(
                "task %s: ui_surface tasks must carry the design-critic extra gate" % tid
            )
        if t.get("risk", "standard") not in RISK_LEVELS:
            raise HarnessError("task %s risk must be one of %s" % (tid, RISK_LEVELS))
        if t.get("ui_surface") and t.get("risk") != "high":
            raise HarnessError(
                "task %s: ui_surface tasks must declare risk high explicitly" % tid
            )
        if not isinstance(t.get("builder_model", ""), str):
            raise HarnessError("task %s builder_model must be a string" % tid)
        for c in t.get("contracts", []):
            if c not in known_contracts and c not in new_contracts:
                raise HarnessError("task %s references undefined contract %s" % (tid, c))

    all_ids = set(existing) | set(new_ids)
    graph = {}
    for tid, tdef in existing.items():
        graph[tid] = tdef.get("deps", [])
    for t in tasks:
        for dep in t.get("deps", []):
            if dep not in all_ids:
                raise HarnessError("task %s depends on unknown task %s" % (t["id"], dep))
        graph[t["id"]] = t.get("deps", [])
    _detect_cycle(graph)

    wave = plan.get("wave", 1)
    ev_name = "PLAN_REVISED" if existing else "WAVE_PLANNED"
    append_event(root, {"ev": ev_name, "wave": wave, "tasks": new_ids, "defaults": defaults})
    for cid, cdef in new_contracts.items():
        if cid in known_contracts:
            continue
        tier = (cdef or {}).get("tier", "PROVISIONAL")
        if tier not in ("PROVISIONAL", "FROZEN"):
            raise HarnessError("contract %s tier must be PROVISIONAL or FROZEN" % cid)
        append_event(root, {"ev": "CONTRACT_DEFINED", "contract": cid, "version": 1,
                            "tier": tier, "text": (cdef or {}).get("text", "")})
    for t in tasks:
        append_event(root, {
            "ev": "PLANNED", "task": t["id"], "goal": t["goal"],
            "deps": t.get("deps", []), "packet": t.get("packet", ""),
            "budget": t.get("budget", {}), "acceptance": t["acceptance"],
            "contracts": t.get("contracts", []),
            "extra_gates": t.get("extra_gates", []),
            "dep_gate": t.get("dep_gate", "integrated"),
            "ui_surface": bool(t.get("ui_surface")),
            "direction_adjacent": bool(t.get("direction_adjacent")),
            "builder_model": t.get("builder_model") or default_builder,
            "risk": t.get("risk", "standard"),
        })
    print("planned %d task(s) in wave %s: %s" % (len(new_ids), wave, ", ".join(new_ids)))


def cmd_ready(args):
    root = find_root()
    print(json.dumps(ready_set(read_events(root))))


def _genesis_gate_ok(events):
    ratified = any(e["ev"] == "CHARTER_RATIFIED" for e in events)
    corpus_pass = any(e["ev"] == "CORPUS_SCENARIO" and e.get("passed") for e in events)
    return ratified, corpus_pass


def cmd_dispatch(args):
    root = find_root()
    events = read_events(root)
    # Parked check runs before the genesis and ready-set checks (C-PARK).
    parked = open_park(events)
    if parked:
        raise HarnessError(
            "run is parked since %s (%s); `harness unpark` before dispatching"
            % (parked["ts"], parked.get("reason", ""))
        )
    s = session(root)
    if s["mode"] == "genesis":
        ratified, corpus_pass = _genesis_gate_ok(events)
        if not (ratified and corpus_pass):
            missing = []
            if not ratified:
                missing.append("a ratified charter (`harness charter ratify --by <operator>`)")
            if not corpus_pass:
                missing.append("at least one passing corpus scenario (`harness corpus record`)")
            raise HarnessError(
                "structural refusal: genesis mode blocks dispatch until the walking skeleton exists; missing " + " and ".join(missing)
            )

    tid = args.task
    defs = task_defs(events)
    if tid not in defs:
        raise HarnessError("task %s is not planned" % tid)
    if defs[tid].get("ui_surface"):
        da = manifest(root).get("direction_artifact")
        if not da or not (root / da).exists():
            raise HarnessError(
                "structural refusal: ui_surface task %s requires a direction artifact; register one with `harness direction <path>`" % tid
            )
    state = derive_states(events).get(tid)

    if state == "planned":
        if tid not in ready_set(events):
            raise HarnessError("task %s is not in the ready set (unsatisfied deps)" % tid)
    elif state == "escalated":
        esc_seq = latest_escalation_seq(events, tid)
        if esc_seq is None or not decision_after(events, tid, esc_seq):
            raise HarnessError(
                "task %s is escalated; record a decision first (`harness decide %s ...`)" % (tid, tid)
            )
    else:
        raise HarnessError("task %s is %s; dispatch requires planned or escalated" % (tid, state))

    attempt = attempts_of(events, tid) + 1
    append_event(root, {"ev": "DISPATCHED", "task": tid, "agent": args.agent,
                        "base_sha": git_sha(root), "attempt": attempt})
    events = read_events(root)
    set_state(root, events, tid, "dispatched")
    print("dispatched %s to %s (attempt %d)" % (tid, args.agent, attempt))


def cmd_run_acceptance(args):
    root = find_root()
    events = read_events(root)
    tid = args.task
    defs = task_defs(events)
    if tid not in defs:
        raise HarnessError("task %s is not planned" % tid)
    if derive_states(events).get(tid) != "dispatched":
        raise HarnessError("task %s is not dispatched; acceptance runs during a dispatch" % tid)
    attempt = attempts_of(events, tid)
    results = []
    all_pass = True
    for cmd in defs[tid]["acceptance"]:
        try:
            r = subprocess.run(cmd, shell=True, cwd=str(root), capture_output=True,
                               text=True, timeout=ACCEPTANCE_TIMEOUT)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            rc = 124
        results.append({"cmd": cmd, "rc": rc})
        if rc != 0:
            all_pass = False
    append_event(root, {"ev": "ACCEPTANCE_RAN", "task": tid, "attempt": attempt,
                        "results": results, "all_pass": all_pass})
    for r in results:
        print("%s  rc=%d" % (r["cmd"], r["rc"]))
    if not all_pass:
        raise HarnessError("acceptance failed for %s" % tid)
    print("acceptance passed for %s (attempt %d)" % (tid, attempt))


def _validate_digest(root, events, tid, digest, tdef):
    problems = []
    if digest.get("task") != tid:
        problems.append("digest task field (%s) does not match %s" % (digest.get("task"), tid))
    status = digest.get("status")
    if status not in DIGEST_STATUSES:
        problems.append("status must be one of %s" % (DIGEST_STATUSES,))
    if status == "done":
        if not isinstance(digest.get("changed"), list):
            problems.append("done digest needs a changed list")
        if not isinstance(digest.get("contracts"), dict):
            problems.append("done digest needs a contracts map (id -> version built against)")
        report = digest.get("report")
        if not report or not (root / report).exists():
            problems.append("done digest needs a report path that exists")
        if tdef.get("ui_surface"):
            evidence = digest.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                problems.append("ui_surface done digest needs a non-empty evidence list (rendered screenshots/video)")
            else:
                for p in evidence:
                    if not (root / p).exists():
                        problems.append("evidence path missing: %s" % p)
            hv = digest.get("human_verify")
            if not isinstance(hv, list) or not hv or not all(
                isinstance(i, dict) and i.get("item") for i in hv
            ):
                problems.append(
                    "ui_surface done digest needs a human_verify list of {item, expected} entries"
                )
    elif status is not None:
        if not digest.get("detail"):
            problems.append("escalation digest needs a detail field")
        if status == "needs-decision":
            opts = digest.get("options")
            if not isinstance(opts, list) or len(opts) < 2:
                problems.append("needs-decision digest needs an options list with at least two entries")
    return problems


def cmd_digest(args):
    root = find_root()
    events = read_events(root)
    tid = args.task
    defs = task_defs(events)
    if tid not in defs:
        raise HarnessError("task %s is not planned" % tid)
    if derive_states(events).get(tid) != "dispatched":
        raise HarnessError("task %s is not dispatched; a digest closes an active dispatch" % tid)
    digest = load_json(Path(args.file), "digest")
    problems = _validate_digest(root, events, tid, digest, defs[tid])
    if problems:
        append_event(root, {"ev": "DIGEST_REJECTED", "task": tid, "problems": problems})
        raise HarnessError("digest rejected: " + "; ".join(problems))

    attempt = attempts_of(events, tid)
    status = digest["status"]
    append_event(root, {"ev": "DIGEST", "task": tid, "attempt": attempt, "status": status,
                        "digest": digest})
    events = read_events(root)
    if status != "done":
        set_state(root, events, tid, "escalated")
        print("escalation recorded for %s: %s" % (tid, status))
        return

    set_state(root, events, tid, "in_gate")
    if digest.get("human_verify"):
        append_event(root, {"ev": "HUMAN_VERIFY_REQUESTED", "task": tid, "attempt": attempt,
                            "items": [i["item"] for i in digest["human_verify"]]})
    append_event(root, {"ev": "GATE_RESULT", "task": tid, "attempt": attempt,
                        "gate": "digest-valid", "result": "pass"})
    acc_ok = any(
        e["ev"] == "ACCEPTANCE_RAN" and e["task"] == tid
        and e.get("attempt") == attempt and e.get("all_pass")
        for e in events
    )
    append_event(root, {"ev": "GATE_RESULT", "task": tid, "attempt": attempt,
                        "gate": "acceptance-ran", "result": "pass" if acc_ok else "fail"})
    if not acc_ok:
        events = read_events(root)
        set_state(root, events, tid, "dispatched")
        raise HarnessError(
            "digest for %s claims done but acceptance has not passed this attempt; run `harness run-acceptance %s`" % (tid, tid)
        )
    print("digest accepted for %s; awaiting critic and verifier gates" % tid)


def cmd_gate(args):
    root = find_root()
    events = read_events(root)
    tid = args.task
    defs = task_defs(events)
    if tid not in defs:
        raise HarnessError("task %s is not planned" % tid)
    if derive_states(events).get(tid) != "in_gate":
        raise HarnessError("task %s is not in gate" % tid)
    gate = args.gate
    req = required_gates(defs[tid])
    if gate not in req:
        raise HarnessError("gate %s is not required for %s (required: %s)" % (gate, tid, req))
    if gate in MACHINE_GATES:
        raise HarnessError("gate %s is machine-owned and recorded by the CLI itself" % gate)
    attempt = attempts_of(events, tid)
    append_event(root, {"ev": "GATE_RESULT", "task": tid, "attempt": attempt,
                        "gate": gate, "result": args.result, "reason": args.reason or ""})
    events = read_events(root)
    if args.result == "fail":
        set_state(root, events, tid, "dispatched")
        print("gate %s failed for %s; task returned to dispatched for rework" % (gate, tid))
        return
    latest = gate_results(events, tid, attempt)
    if all(latest.get(g) == "pass" for g in req):
        set_state(root, events, tid, "done")
        print("all gates passed; %s is done" % tid)
    else:
        pending = [g for g in req if latest.get(g) != "pass"]
        print("gate %s passed for %s; pending: %s" % (gate, tid, ", ".join(pending)))


def cmd_decide(args):
    root = find_root()
    append_event(root, {"ev": "DECISION", "task": args.task, "q": args.question,
                        "a": args.answer, "by": session(root)["operator"]})
    print("decision recorded for %s" % args.task)


def _run_metric(root, mdef):
    try:
        r = subprocess.run(mdef["command"], shell=True, cwd=str(root), capture_output=True,
                           text=True, timeout=ACCEPTANCE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise HarnessError("metric command timed out: %s" % mdef["command"])
    if r.returncode != 0:
        raise HarnessError("metric command failed (rc=%d): %s" % (r.returncode, mdef["command"]))
    try:
        return int(r.stdout.strip().split()[-1])
    except (ValueError, IndexError):
        raise HarnessError("metric command must print an integer, got: %r" % r.stdout.strip())


def _last_metric_value(events, mid):
    val = None
    for e in events:
        if e["ev"] == "METRIC" and e["metric"] == mid:
            val = e["value"]
    return val


def _check_metric(root, events, mid, strict=False, record=True):
    m = manifest(root)
    mdef = m["metrics"].get(mid)
    if not mdef:
        raise HarnessError("metric %s is not registered" % mid)
    value = _run_metric(root, mdef)
    last = _last_metric_value(events, mid)
    ok = True
    if last is not None:
        if mdef["direction"] == "down":
            ok = value < last if strict else value <= last
        else:
            ok = value > last if strict else value >= last
    if record:
        append_event(root, {"ev": "METRIC", "metric": mid, "value": value,
                            "previous": last, "ok": ok, "class": mdef.get("class", "coverage")})
    return value, last, ok


def cmd_metric(args):
    root = find_root()
    if args.action == "register":
        if not args.id or not args.command:
            raise HarnessError("metric register needs --id and --command")
        if args.direction not in ("up", "down"):
            raise HarnessError("--direction must be up or down")
        if args.metric_class not in ("correctness", "coverage"):
            raise HarnessError("--class must be correctness or coverage")
        m = manifest(root)
        m["metrics"][args.id] = {"direction": args.direction, "command": args.command,
                                 "class": args.metric_class}
        save_manifest(root, m)
        append_event(root, {"ev": "METRIC_REGISTERED", "metric": args.id,
                            "direction": args.direction, "command": args.command,
                            "class": args.metric_class})
        events = read_events(root)
        value, _, _ = _check_metric(root, events, args.id)
        print("registered metric %s (direction=%s, baseline=%d)" % (args.id, args.direction, value))
    elif args.action == "check":
        events = read_events(root)
        m = manifest(root)
        ids = [args.id] if args.id else sorted(m["metrics"])
        failed = []
        for mid in ids:
            value, last, ok = _check_metric(root, events, mid, strict=args.strict)
            print("%s: %s -> %s  %s" % (mid, last, value, "ok" if ok else "VIOLATION"))
            if not ok:
                failed.append(mid)
            events = read_events(root)
        if failed:
            raise HarnessError("monotonicity violated: %s" % ", ".join(failed))
    else:
        raise HarnessError("metric action must be register or check")


def cmd_direction(args):
    root = find_root()
    if not (root / args.path).exists():
        raise HarnessError("direction artifact not found: %s" % args.path)
    m = manifest(root)
    m["direction_artifact"] = args.path
    save_manifest(root, m)
    append_event(root, {"ev": "DIRECTION_SET", "path": args.path})
    print("direction artifact registered: %s" % args.path)


def cmd_human_verify(args):
    root = find_root()
    events = read_events(root)
    tid = args.task
    seq, digest = latest_done_digest(events, tid)
    if seq is None:
        raise HarnessError("task %s has no accepted done digest" % tid)
    items = digest.get("human_verify", [])
    if not (0 <= args.item < len(items)):
        raise HarnessError("task %s has %d human-verify items; index %d is out of range"
                           % (tid, len(items), args.item))
    append_event(root, {"ev": "HUMAN_VERIFY_RESULT", "task": tid, "item_index": args.item,
                        "item": items[args.item]["item"], "passed": args.passed,
                        "note": args.note or "", "digest_seq": seq})
    print("human-verify item %d for %s: %s" % (args.item, tid, "pass" if args.passed else "FAIL"))


def cmd_flag_ack(args):
    root = find_root()
    events = read_events(root)
    tid = args.task
    seq, digest = latest_done_digest(events, tid)
    if seq is None:
        raise HarnessError("task %s has no accepted done digest" % tid)
    flags = digest.get("flags", [])
    if not (0 <= args.flag < len(flags)):
        raise HarnessError("task %s has %d flags; index %d is out of range"
                           % (tid, len(flags), args.flag))
    append_event(root, {"ev": "FLAG_ACK", "task": tid, "flag_index": args.flag,
                        "flag": flags[args.flag], "resolution": args.resolution,
                        "note": args.note or "", "digest_seq": seq})
    print("flag %d on %s acknowledged: %s" % (args.flag, tid, args.resolution))


def cmd_pick(args):
    root = find_root()
    events = read_events(root)
    if args.task not in task_defs(events):
        raise HarnessError("task %s is not planned" % args.task)
    options = [o.strip() for o in args.options.split("|") if o.strip()]
    if len(options) < 2:
        raise HarnessError("pick needs at least two options (separate with |)")
    if args.choice not in options:
        raise HarnessError("choice %r is not among the options %s" % (args.choice, options))
    append_event(root, {"ev": "PICK", "task": args.task, "options": options,
                        "choice": args.choice, "note": args.note or ""})
    print("pick recorded for %s: %s" % (args.task, args.choice))


def cmd_reopen(args):
    root = find_root()
    events = read_events(root)
    tid = args.task
    if derive_states(events).get(tid) != "done":
        raise HarnessError("task %s is not done; reopen returns a done task to rework" % tid)
    append_event(root, {"ev": "REOPENED", "task": tid, "reason": args.reason or ""})
    events = read_events(root)
    set_state(root, events, tid, "dispatched")
    print("reopened %s for rework" % tid)


def _integration_holds(root, events, tid, tdef):
    """Human-judgment holds that block integration. Returns a list of reasons."""
    holds = []
    seq, digest = latest_done_digest(events, tid)
    if seq is None:
        return ["no accepted done digest"]

    flags = digest.get("flags", [])
    acked = {e["flag_index"] for e in events
             if e["ev"] == "FLAG_ACK" and e["task"] == tid and e.get("digest_seq") == seq}
    unacked = [i for i in range(len(flags)) if i not in acked]
    if unacked:
        holds.append("unacknowledged flags %s (use `harness flag-ack %s --flag N --resolution ...`)"
                     % (unacked, tid))

    hv = digest.get("human_verify", [])
    latest_result = {}
    for e in events:
        if e["ev"] == "HUMAN_VERIFY_RESULT" and e["task"] == tid and e.get("digest_seq") == seq:
            latest_result[e["item_index"]] = e["passed"]
    pending = [i for i in range(len(hv)) if i not in latest_result]
    failed = [i for i in range(len(hv)) if latest_result.get(i) is False]
    if pending:
        holds.append("human-verify items pending: %s" % pending)
    if failed:
        holds.append("human-verify items FAILED: %s (fix and `harness reopen %s`, or re-verify)"
                     % (failed, tid))

    if tdef.get("direction_adjacent"):
        if not any(e["ev"] == "PICK" and e["task"] == tid for e in events):
            holds.append("direction_adjacent task has no recorded pairwise pick (`harness pick %s ...`)" % tid)
    return holds


def cmd_integrate(args):
    root = find_root()
    events = read_events(root)
    tid = args.task
    if derive_states(events).get(tid) != "done":
        raise HarnessError("task %s is not done; only done tasks integrate" % tid)
    holds = _integration_holds(root, events, tid, task_defs(events)[tid])
    if holds:
        append_event(root, {"ev": "INTEGRATION_BLOCKED", "task": tid, "holds": holds})
        raise HarnessError("integration blocked for %s: %s" % (tid, "; ".join(holds)))
    m = manifest(root)
    failed = []
    for mid in sorted(m["metrics"]):
        _, _, ok = _check_metric(root, events, mid)
        if not ok:
            failed.append(mid)
        events = read_events(root)
    if failed:
        append_event(root, {"ev": "INTEGRATION_BLOCKED", "task": tid, "metrics": failed})
        raise HarnessError("integration blocked for %s: metric violation(s) %s" % (tid, ", ".join(failed)))
    order = 1 + sum(1 for e in events if e["ev"] == "INTEGRATED")
    append_event(root, {"ev": "INTEGRATED", "task": tid, "sha": args.sha or git_sha(root),
                        "order": order})
    events = read_events(root)
    set_state(root, events, tid, "integrated")
    print("integrated %s (order %d)" % (tid, order))


def cmd_deploy(args):
    root = find_root()
    events = read_events(root)
    tid = args.task
    if derive_states(events).get(tid) != "integrated":
        raise HarnessError("task %s is not integrated; deploy follows integration" % tid)
    append_event(root, {"ev": "DEPLOYED", "task": tid, "note": args.note or ""})
    events = read_events(root)
    set_state(root, events, tid, "deployed")
    print("deployed %s" % tid)


def cmd_contract(args):
    root = find_root()
    events = read_events(root)
    if args.action != "amend":
        raise HarnessError("contract action must be amend")
    cid = args.id
    version, tier = None, None
    for e in events:
        if e["ev"] in ("CONTRACT_DEFINED", "CONTRACT_AMENDED") and e["contract"] == cid:
            version, tier = e["version"], e.get("tier", "PROVISIONAL")
    if version is None:
        raise HarnessError("contract %s is not defined" % cid)
    new_tier = args.tier or tier
    append_event(root, {"ev": "CONTRACT_AMENDED", "contract": cid, "version": version + 1,
                        "tier": new_tier, "text": args.text or ""})
    states = derive_states(events)
    reverify = []
    for tid, tdef in task_defs(events).items():
        if cid not in tdef.get("contracts", []):
            continue
        st = states.get(tid)
        if tier == "FROZEN":
            hit = st in ("dispatched", "escalated", "in_gate", "done", "integrated", "deployed")
        else:
            hit = st in ("dispatched", "escalated", "in_gate", "done")
        if hit:
            reverify.append(tid)
            append_event(root, {"ev": "REVERIFY", "task": tid, "contract": cid,
                                "contract_version": version + 1})
    print("amended %s to v%d (%s); reverify: %s" % (cid, version + 1, new_tier, ", ".join(reverify) or "none"))


def cmd_charter(args):
    root = find_root()
    if args.action != "ratify":
        raise HarnessError("charter action must be ratify")
    if not args.by:
        raise HarnessError("charter ratify needs --by <operator>; ratification is a human act")
    append_event(root, {"ev": "CHARTER_RATIFIED", "by": args.by, "path": args.path or ""})
    print("charter ratified by %s" % args.by)


def cmd_corpus(args):
    root = find_root()
    if args.action != "record":
        raise HarnessError("corpus action must be record")
    events = read_events(root)
    known = {e["scenario"] for e in events if e["ev"] == "CORPUS_SCENARIO"}
    append_event(root, {"ev": "CORPUS_SCENARIO", "scenario": args.id,
                        "passed": args.passed, "new": args.id not in known})
    print("corpus scenario %s recorded (%s)" % (args.id, "pass" if args.passed else "fail"))


def cmd_checkout(args):
    root = find_root()
    append_event(root, {"ev": "CHECKOUT", "path": args.path, "reason": args.reason or ""})
    print("checkout logged: %s" % args.path)


def cmd_usage(args):
    root = find_root()
    events = read_events(root)
    tid = args.task
    if tid != "run" and tid not in task_defs(events):
        raise HarnessError(
            "task %s is not planned (use the literal `run` for orchestrator-level usage)" % tid
        )
    append_event(root, {"ev": "USAGE", "task": tid, "tokens": args.tokens,
                        "duration_ms": args.duration_ms, "role": args.role,
                        "model": args.model or "", "agent": args.agent or ""})
    print("usage recorded for %s: %d tokens, %d ms (%s)"
          % (tid, args.tokens, args.duration_ms, args.role))


def cmd_hypothesis(args):
    root = find_root()
    events = read_events(root)
    if args.task not in task_defs(events):
        raise HarnessError("task %s is not planned" % args.task)
    if not args.reason.strip():
        raise HarnessError("hypothesis needs a non-empty --reason")
    append_event(root, {"ev": "COUNTERFACTUAL", "task": args.task, "scheme": args.scheme,
                        "verdict": args.verdict, "reason": args.reason})
    print("hypothesis recorded for %s: %s -> %s" % (args.task, args.scheme, args.verdict))


def cmd_park(args):
    root = find_root()
    events = read_events(root)
    if not args.reason.strip():
        raise HarnessError("park needs a non-empty --reason")
    parked = open_park(events)
    if parked:
        raise HarnessError("run is already parked (since %s)" % parked["ts"])
    append_event(root, {"ev": "PARKED", "reason": args.reason})
    print("parked: %s" % args.reason)


def cmd_unpark(args):
    root = find_root()
    events = read_events(root)
    parked = open_park(events)
    if parked is None:
        raise HarnessError("run is not parked")
    append_event(root, {"ev": "UNPARKED"})
    print("unparked (was parked since %s)" % parked["ts"])


def cmd_status(args):
    root = find_root()
    events = read_events(root)
    s = session(root)
    states = derive_states(events)
    defs = task_defs(events)
    parked = open_park(events)
    if parked:
        print("PARKED since %s: %s" % (parked["ts"], parked.get("reason", "")))
    print("mode=%s operator=%s session=%s" % (s["mode"], s["operator"], s["session_id"][:8]))
    if not defs:
        print("no tasks planned")
        return
    agents = {}
    for e in events:
        if e["ev"] == "DISPATCHED":
            agents[e["task"]] = e["agent"]
    print("%-8s %-11s %-8s %-10s %s" % ("task", "state", "attempts", "agent", "goal"))
    for tid in sorted(defs):
        print("%-8s %-11s %-8d %-10s %s" % (
            tid, states.get(tid, "?"), attempts_of(events, tid),
            agents.get(tid, "-"), defs[tid]["goal"][:60]))
    ready = ready_set(events)
    if ready:
        print("ready: %s" % ", ".join(ready))


def cmd_metrics(args):
    root = find_root()
    events = read_events(root)
    states = derive_states(events)
    defs = task_defs(events)

    by_state = {}
    for tid in defs:
        st = states.get(tid, "?")
        by_state[st] = by_state.get(st, 0) + 1

    rework = sum(max(0, attempts_of(events, tid) - 1) for tid in defs)
    escalations = {}
    for e in events:
        if e["ev"] == "DIGEST" and e.get("status") != "done":
            escalations[e["status"]] = escalations.get(e["status"], 0) + 1

    gates = {}
    for e in events:
        if e["ev"] == "GATE_RESULT":
            g = gates.setdefault(e["gate"], {"pass": 0, "fail": 0})
            g[e["result"]] += 1

    done_count = sum(1 for e in events if e["ev"] == "STATE" and e["state"] == "done")
    zero_catch = [
        g for g, r in gates.items()
        if g not in MACHINE_GATES and r["fail"] == 0 and done_count >= 20
    ]

    hv_results = [e for e in events if e["ev"] == "HUMAN_VERIFY_RESULT"]
    picks = sum(1 for e in events if e["ev"] == "PICK")
    flag_acks = [e for e in events if e["ev"] == "FLAG_ACK"]

    # Calibration: tasks where the design-critic passed but a human later failed
    # a human-verify item. This is the judge-vs-human disagreement signal.
    critic_passed = {e["task"] for e in events
                     if e["ev"] == "GATE_RESULT" and e["gate"] == "design-critic"
                     and e["result"] == "pass"}
    human_failed = {e["task"] for e in hv_results if not e["passed"]}
    disagreements = sorted(critic_passed & human_failed)

    usage_events = [e for e in events if e["ev"] == "USAGE"]
    total_tokens = sum(e["tokens"] for e in usage_events)
    tokens_by_role = {}
    tokens_by_task = {}
    for e in usage_events:
        tokens_by_role[e["role"]] = tokens_by_role.get(e["role"], 0) + e["tokens"]
        tokens_by_task[e["task"]] = tokens_by_task.get(e["task"], 0) + e["tokens"]

    hypotheses = {}
    for e in events:
        if e["ev"] == "COUNTERFACTUAL":
            by_verdict = hypotheses.setdefault(e["scheme"], {})
            by_verdict[e["verdict"]] = by_verdict.get(e["verdict"], 0) + 1

    # Parked minutes count closed PARKED/UNPARKED pairs only; an open park
    # contributes zero and is surfaced by the status banner instead.
    park_count = 0
    parked_minutes = 0.0
    park_open_ts = None
    for e in events:
        if e["ev"] == "PARKED":
            park_count += 1
            park_open_ts = e["ts"]
        elif e["ev"] == "UNPARKED" and park_open_ts is not None:
            parked_minutes += max(0.0, ts_epoch(e["ts"]) - ts_epoch(park_open_ts)) / 60.0
            park_open_ts = None

    out = {
        "tasks": len(defs),
        "by_state": by_state,
        "rework_dispatches": rework,
        "escalations": escalations,
        "gate_results": gates,
        "decisions": sum(1 for e in events if e["ev"] == "DECISION"),
        "checkouts": sum(1 for e in events if e["ev"] == "CHECKOUT"),
        "reverify_pending": sum(1 for e in events if e["ev"] == "REVERIFY"),
        "zero_catch_suspects": zero_catch,
        "human_verify": {
            "passed": sum(1 for e in hv_results if e["passed"]),
            "failed": sum(1 for e in hv_results if not e["passed"]),
        },
        "picks": picks,
        "flags": {
            "acked": len(flag_acks),
            "resolutions": {r: sum(1 for e in flag_acks if e["resolution"] == r)
                            for r in ("ok", "escalated", "new-task")},
        },
        "calibration": {
            "design_critic_gated_tasks": len(critic_passed),
            "critic_pass_human_fail": disagreements,
        },
        "usage": {
            "total_tokens": total_tokens,
            "tokens_by_role": tokens_by_role,
            "tokens_by_task": tokens_by_task,
            "orchestrator_share": (
                tokens_by_role.get("orchestrator", 0) / total_tokens
                if total_tokens else None
            ),
        },
        "hypotheses": hypotheses,
        "parks": {"count": park_count, "parked_minutes": round(parked_minutes, 2)},
    }
    print(json.dumps(out, indent=2))


def cmd_events(args):
    root = find_root()
    events = read_events(root)
    for e in events[-args.tail:]:
        print(json.dumps(e))


# ---------------------------------------------------------------- entrypoint


def build_parser():
    p = argparse.ArgumentParser(prog="harness", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="initialize a run in the current repo (takes the repo lease)")
    sp.add_argument("--mode", default="standard", choices=MODES)
    sp.add_argument("--operator", default=None)
    sp.add_argument("--task-type", default="general")
    sp.add_argument("--orchestrator-model", default=None,
                    help="orchestrator model id; non-frontier ids refuse without --allow-non-frontier")
    sp.add_argument("--allow-non-frontier", action="store_true",
                    help="proceed with a non-frontier orchestrator model (recorded as non_frontier_ack)")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("release", help="release the repo lease")
    sp.set_defaults(fn=cmd_release)

    sp = sub.add_parser("plan", help="validate and record a plan file (rolling waves add new tasks)")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_plan)

    sp = sub.add_parser("ready", help="print the computed ready set")
    sp.set_defaults(fn=cmd_ready)

    sp = sub.add_parser("dispatch", help="dispatch a ready or decided task to an agent")
    sp.add_argument("task")
    sp.add_argument("--agent", required=True)
    sp.set_defaults(fn=cmd_dispatch)

    sp = sub.add_parser("run-acceptance", help="execute and record a task's acceptance commands")
    sp.add_argument("task")
    sp.set_defaults(fn=cmd_run_acceptance)

    sp = sub.add_parser("digest", help="validate and record a digest file for a dispatched task")
    sp.add_argument("task")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_digest)

    sp = sub.add_parser("gate", help="record a judgment gate result (critic, verifier, extras)")
    sp.add_argument("task")
    sp.add_argument("gate")
    sp.add_argument("result", choices=["pass", "fail"])
    sp.add_argument("--reason", default="")
    sp.set_defaults(fn=cmd_gate)

    sp = sub.add_parser("decide", help="record an orchestrator decision for an escalated task")
    sp.add_argument("task")
    sp.add_argument("--question", required=True)
    sp.add_argument("--answer", required=True)
    sp.set_defaults(fn=cmd_decide)

    sp = sub.add_parser("integrate", help="integrate a done task (runs all registered metrics)")
    sp.add_argument("task")
    sp.add_argument("--sha", default=None)
    sp.set_defaults(fn=cmd_integrate)

    sp = sub.add_parser("deploy", help="mark an integrated task deployed")
    sp.add_argument("task")
    sp.add_argument("--note", default="")
    sp.set_defaults(fn=cmd_deploy)

    sp = sub.add_parser("metric", help="register or check a monotone metric")
    sp.add_argument("action", choices=["register", "check"])
    sp.add_argument("id", nargs="?")
    sp.add_argument("--direction", default="down")
    sp.add_argument("--command", default=None)
    sp.add_argument("--class", dest="metric_class", default="coverage")
    sp.add_argument("--strict", action="store_true")
    sp.set_defaults(fn=cmd_metric)

    sp = sub.add_parser("contract", help="amend a contract (bumps version, emits REVERIFY)")
    sp.add_argument("action", choices=["amend"])
    sp.add_argument("id")
    sp.add_argument("--text", default="")
    sp.add_argument("--tier", default=None, choices=[None, "PROVISIONAL", "FROZEN"])
    sp.set_defaults(fn=cmd_contract)

    sp = sub.add_parser("charter", help="record charter ratification (a human act)")
    sp.add_argument("action", choices=["ratify"])
    sp.add_argument("--by", required=True)
    sp.add_argument("--path", default="")
    sp.set_defaults(fn=cmd_charter)

    sp = sub.add_parser("corpus", help="record a corpus scenario result")
    sp.add_argument("action", choices=["record"])
    sp.add_argument("--id", required=True)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--pass", dest="passed", action="store_true")
    g.add_argument("--fail", dest="passed", action="store_false")
    sp.set_defaults(fn=cmd_corpus)

    sp = sub.add_parser("checkout", help="log an orchestrator evidence checkout")
    sp.add_argument("path")
    sp.add_argument("--reason", default="")
    sp.set_defaults(fn=cmd_checkout)

    sp = sub.add_parser("usage", help="record token/duration telemetry for a task (or the literal `run`)")
    sp.add_argument("task", help="task id, or `run` for orchestrator-level usage")
    sp.add_argument("--tokens", type=int, required=True)
    sp.add_argument("--duration-ms", type=int, required=True)
    sp.add_argument("--role", required=True, choices=list(USAGE_ROLES))
    sp.add_argument("--model", default="")
    sp.add_argument("--agent", default="")
    sp.set_defaults(fn=cmd_usage)

    sp = sub.add_parser("hypothesis", help="record a counterfactual scheme hypothesis for a task")
    sp.add_argument("task")
    sp.add_argument("--scheme", required=True, choices=list(HYPOTHESIS_SCHEMES))
    sp.add_argument("--verdict", required=True, choices=list(HYPOTHESIS_VERDICTS))
    sp.add_argument("--reason", required=True)
    sp.set_defaults(fn=cmd_hypothesis)

    sp = sub.add_parser("park", help="park the run (dispatch refuses until unpark)")
    sp.add_argument("--reason", required=True)
    sp.set_defaults(fn=cmd_park)

    sp = sub.add_parser("unpark", help="resume a parked run")
    sp.set_defaults(fn=cmd_unpark)

    sp = sub.add_parser("direction", help="register the direction artifact required by ui_surface tasks")
    sp.add_argument("path")
    sp.set_defaults(fn=cmd_direction)

    sp = sub.add_parser("human-verify", help="record an operator result for a digest's human-verify item")
    sp.add_argument("task")
    sp.add_argument("--item", type=int, required=True)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--pass", dest="passed", action="store_true")
    g.add_argument("--fail", dest="passed", action="store_false")
    sp.add_argument("--note", default="")
    sp.set_defaults(fn=cmd_human_verify)

    sp = sub.add_parser("flag-ack", help="acknowledge a digest flag before integration")
    sp.add_argument("task")
    sp.add_argument("--flag", type=int, required=True)
    sp.add_argument("--resolution", required=True, choices=["ok", "escalated", "new-task"])
    sp.add_argument("--note", default="")
    sp.set_defaults(fn=cmd_flag_ack)

    sp = sub.add_parser("pick", help="record an operator's pairwise pick for a direction-adjacent task")
    sp.add_argument("task")
    sp.add_argument("--options", required=True, help="pipe-separated, e.g. 'variant-a|variant-b'")
    sp.add_argument("--choice", required=True)
    sp.add_argument("--note", default="")
    sp.set_defaults(fn=cmd_pick)

    sp = sub.add_parser("reopen", help="return a done task to rework (e.g. after a human-verify fail)")
    sp.add_argument("task")
    sp.add_argument("--reason", default="")
    sp.set_defaults(fn=cmd_reopen)

    sp = sub.add_parser("status", help="derived state table")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("metrics", help="telemetry computed from the ledger")
    sp.set_defaults(fn=cmd_metrics)

    sp = sub.add_parser("events", help="print recent ledger events")
    sp.add_argument("--tail", type=int, default=20)
    sp.set_defaults(fn=cmd_events)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.fn(args)
        return 0
    except HarnessError as e:
        print("harness: error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
