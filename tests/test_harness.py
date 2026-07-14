"""End-to-end tests for the harness CLI, driven through the real entrypoint.

Each test operates on a throwaway git repo so the ledger, lease, and state
machine are exercised exactly as a real run would exercise them.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def harness(args, cwd):
    env = dict(os.environ, PYTHONPATH=str(REPO))
    return subprocess.run(
        [sys.executable, "-m", "harness_cli"] + args,
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


BASIC_PLAN = {
    "wave": 1,
    "contracts": {"C1": {"tier": "PROVISIONAL", "text": "demo contract"}},
    "tasks": [
        {"id": "T1", "goal": "foundation", "deps": [], "acceptance": ["true"],
         "contracts": ["C1"]},
        {"id": "T2", "goal": "feature on top", "deps": ["T1"], "acceptance": ["true"],
         "contracts": ["C1"]},
    ],
}


class HarnessBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=str(self.root), check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "init"],
                       cwd=str(self.root), check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def ok(self, args):
        r = harness(args, self.root)
        self.assertEqual(r.returncode, 0, "expected success for %s\nstdout: %s\nstderr: %s"
                         % (args, r.stdout, r.stderr))
        return r

    def fail(self, args, needle=None):
        r = harness(args, self.root)
        self.assertNotEqual(r.returncode, 0, "expected failure for %s\nstdout: %s" % (args, r.stdout))
        if needle:
            self.assertIn(needle, r.stderr)
        return r

    def state_of(self, task):
        r = self.ok(["status"])
        for line in r.stdout.splitlines():
            parts = line.split()
            if parts and parts[0] == task:
                return parts[1]
        return None

    def submit_done_digest(self, task):
        report = self.root / ".harness" / "reports" / ("%s.md" % task)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("report for %s\n" % task)
        digest_path = self.root / ".harness" / "digests" / ("%s.json" % task)
        write_json(digest_path, {"task": task, "status": "done", "changed": ["x.py"],
                                 "contracts": {"C1": 1}, "flags": [],
                                 "report": ".harness/reports/%s.md" % task})
        return str(digest_path.relative_to(self.root))


class TestInitAndLease(HarnessBase):
    def test_init_and_lease_refusal(self):
        self.ok(["init"])
        self.fail(["init"], "lease held")
        self.ok(["release"])

    def test_commands_refuse_without_init(self):
        self.fail(["ready"], "no .harness")


class TestPlanValidation(HarnessBase):
    def setUp(self):
        super().setUp()
        self.ok(["init"])

    def plan(self, plan_obj):
        write_json(self.root / "plan.json", plan_obj)
        return harness(["plan", "plan.json"], self.root)

    def test_valid_plan(self):
        r = self.plan(BASIC_PLAN)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cycle_rejected(self):
        plan = {"tasks": [
            {"id": "A", "goal": "a", "deps": ["B"], "acceptance": ["true"]},
            {"id": "B", "goal": "b", "deps": ["A"], "acceptance": ["true"]},
        ]}
        r = self.plan(plan)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cycle", r.stderr)

    def test_missing_acceptance_rejected(self):
        plan = {"tasks": [{"id": "A", "goal": "a", "deps": [], "acceptance": []}]}
        r = self.plan(plan)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("acceptance", r.stderr)

    def test_unknown_dep_rejected(self):
        plan = {"tasks": [{"id": "A", "goal": "a", "deps": ["ZZ"], "acceptance": ["true"]}]}
        r = self.plan(plan)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown task", r.stderr)

    def test_undefined_contract_rejected(self):
        plan = {"tasks": [{"id": "A", "goal": "a", "deps": [], "acceptance": ["true"],
                           "contracts": ["NOPE"]}]}
        r = self.plan(plan)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("undefined contract", r.stderr)

    def test_floor_gate_cannot_be_extra(self):
        plan = {"tasks": [{"id": "A", "goal": "a", "deps": [], "acceptance": ["true"],
                           "extra_gates": ["critic"]}]}
        r = self.plan(plan)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("floor gate", r.stderr)


class TestLifecycle(HarnessBase):
    def setUp(self):
        super().setUp()
        self.ok(["init"])
        write_json(self.root / "plan.json", BASIC_PLAN)
        self.ok(["plan", "plan.json"])

    def test_full_lifecycle(self):
        r = self.ok(["ready"])
        self.assertEqual(json.loads(r.stdout), ["T1"])

        self.fail(["dispatch", "T2", "--agent", "builder-1"], "not in the ready set")
        self.ok(["dispatch", "T1", "--agent", "builder-1"])
        self.fail(["dispatch", "T1", "--agent", "builder-1"], "dispatch requires planned or escalated")

        # A done digest without acceptance evidence is refused and rework begins.
        rel = self.submit_done_digest("T1")
        self.fail(["digest", "T1", rel], "acceptance has not passed")
        self.assertEqual(self.state_of("T1"), "dispatched")

        self.ok(["run-acceptance", "T1"])
        self.ok(["digest", "T1", rel])
        self.assertEqual(self.state_of("T1"), "in_gate")

        # Machine gates cannot be recorded by hand.
        self.fail(["gate", "T1", "acceptance-ran", "pass"], "machine-owned")

        self.ok(["gate", "T1", "critic", "pass", "--reason", "conforms to C1"])
        self.assertEqual(self.state_of("T1"), "in_gate")
        self.ok(["gate", "T1", "verifier", "pass", "--reason", "evidence attached"])
        self.assertEqual(self.state_of("T1"), "done")

        # Monotone metric: register at 5, improve to 4, integrate cleanly.
        (self.root / "count.txt").write_text("5\n")
        self.ok(["metric", "register", "refs", "--direction", "down",
                 "--command", "cat count.txt", "--class", "coverage"])
        (self.root / "count.txt").write_text("4\n")
        self.ok(["integrate", "T1"])
        self.assertEqual(self.state_of("T1"), "integrated")

        r = self.ok(["ready"])
        self.assertEqual(json.loads(r.stdout), ["T2"])

        # Metric moving the wrong way blocks integration for the next task.
        self.ok(["dispatch", "T2", "--agent", "builder-2"])
        self.ok(["run-acceptance", "T2"])
        rel2 = self.submit_done_digest("T2")
        self.ok(["digest", "T2", rel2])
        self.ok(["gate", "T2", "critic", "pass"])
        self.ok(["gate", "T2", "verifier", "pass"])
        (self.root / "count.txt").write_text("9\n")
        self.fail(["integrate", "T2"], "metric violation")
        (self.root / "count.txt").write_text("3\n")
        self.ok(["integrate", "T2"])
        self.ok(["deploy", "T2", "--note", "smoke ok"])
        self.assertEqual(self.state_of("T2"), "deployed")

        r = self.ok(["metrics"])
        m = json.loads(r.stdout)
        self.assertEqual(m["tasks"], 2)
        self.assertEqual(m["by_state"].get("deployed"), 1)

    def test_gate_fail_returns_to_rework(self):
        self.ok(["dispatch", "T1", "--agent", "builder-1"])
        self.ok(["run-acceptance", "T1"])
        rel = self.submit_done_digest("T1")
        self.ok(["digest", "T1", rel])
        self.ok(["gate", "T1", "critic", "fail", "--reason", "violates C1"])
        self.assertEqual(self.state_of("T1"), "dispatched")
        # Resubmission works within the same attempt after fixes.
        self.ok(["run-acceptance", "T1"])
        self.ok(["digest", "T1", rel])
        self.ok(["gate", "T1", "critic", "pass"])
        self.ok(["gate", "T1", "verifier", "pass"])
        self.assertEqual(self.state_of("T1"), "done")

    def test_escalation_requires_decision(self):
        self.ok(["dispatch", "T1", "--agent", "builder-1"])
        digest_path = self.root / ".harness" / "digests" / "T1.json"
        write_json(digest_path, {"task": "T1", "status": "needs-decision",
                                 "detail": "two viable auth shapes",
                                 "options": ["shim", "rewrite"]})
        self.ok(["digest", "T1", ".harness/digests/T1.json"])
        self.assertEqual(self.state_of("T1"), "escalated")
        self.fail(["dispatch", "T1", "--agent", "builder-1"], "record a decision first")
        self.ok(["decide", "T1", "--question", "auth shape", "--answer", "shim"])
        self.ok(["dispatch", "T1", "--agent", "builder-1"])
        self.assertEqual(self.state_of("T1"), "dispatched")

    def test_invalid_digest_rejected(self):
        self.ok(["dispatch", "T1", "--agent", "builder-1"])
        digest_path = self.root / ".harness" / "digests" / "T1.json"
        write_json(digest_path, {"task": "T1", "status": "done"})
        self.fail(["digest", "T1", ".harness/digests/T1.json"], "digest rejected")

    def test_contract_amend_emits_reverify(self):
        self.ok(["dispatch", "T1", "--agent", "builder-1"])
        r = self.ok(["contract", "amend", "C1", "--text", "v2 wording"])
        self.assertIn("T1", r.stdout)
        rr = self.ok(["metrics"])
        self.assertEqual(json.loads(rr.stdout)["reverify_pending"], 1)


class TestGenesisRefusal(HarnessBase):
    def test_dispatch_blocked_until_walking_skeleton(self):
        self.ok(["init", "--mode", "genesis"])
        write_json(self.root / "plan.json", {"tasks": [
            {"id": "G1", "goal": "skeleton", "deps": [], "acceptance": ["true"]},
        ]})
        self.ok(["plan", "plan.json"])
        self.fail(["dispatch", "G1", "--agent", "builder-1"], "structural refusal")
        self.ok(["charter", "ratify", "--by", "operator"])
        self.fail(["dispatch", "G1", "--agent", "builder-1"], "structural refusal")
        self.ok(["corpus", "record", "--id", "e2e-smoke", "--pass"])
        self.ok(["dispatch", "G1", "--agent", "builder-1"])


if __name__ == "__main__":
    unittest.main()
