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

    def ledger_events(self):
        events = []
        with open(self.root / ".harness" / "ledger.jsonl") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def seed_event(self, ev):
        """Append a raw event line, for tests that need controlled timestamps."""
        with open(self.root / ".harness" / "ledger.jsonl", "a") as f:
            f.write(json.dumps(ev) + "\n")

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


UI_PLAN = {
    "wave": 1,
    "contracts": {"C1": {"tier": "PROVISIONAL", "text": "demo"}},
    "tasks": [
        {"id": "U1", "goal": "hero tile rework", "deps": [], "acceptance": ["true"],
         "contracts": ["C1"], "ui_surface": True, "direction_adjacent": True,
         "extra_gates": ["design-critic"], "risk": "high"},
    ],
}


class TestTasteLayer(HarnessBase):
    def setUp(self):
        super().setUp()
        self.ok(["init"])

    def plan_ui(self):
        write_json(self.root / "plan.json", UI_PLAN)
        self.ok(["plan", "plan.json"])

    def submit_ui_digest(self, task, flags=None, with_evidence=True, with_hv=True):
        report = self.root / ".harness" / "reports" / ("%s.md" % task)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("report\n")
        d = {"task": task, "status": "done", "changed": ["x.scss"],
             "contracts": {"C1": 1}, "flags": flags or [],
             "report": ".harness/reports/%s.md" % task}
        if with_evidence:
            shot = self.root / ".harness" / "reports" / ("%s-shot.png" % task)
            shot.write_bytes(b"png")
            d["evidence"] = [".harness/reports/%s-shot.png" % task]
        if with_hv:
            d["human_verify"] = [{"item": "sound feels calm", "expected": "no machine-gun plops"}]
        path = self.root / ".harness" / "digests" / ("%s.json" % task)
        write_json(path, d)
        return str(path.relative_to(self.root))

    def test_ui_task_requires_design_critic_gate(self):
        bad = {"tasks": [{"id": "U9", "goal": "ui", "deps": [], "acceptance": ["true"],
                          "ui_surface": True}]}
        write_json(self.root / "plan.json", bad)
        r = harness(["plan", "plan.json"], self.root)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("design-critic", r.stderr)

    def test_direction_adjacent_implies_ui_surface(self):
        bad = {"tasks": [{"id": "U9", "goal": "ui", "deps": [], "acceptance": ["true"],
                          "direction_adjacent": True}]}
        write_json(self.root / "plan.json", bad)
        r = harness(["plan", "plan.json"], self.root)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("implies ui_surface", r.stderr)

    def test_ui_dispatch_requires_direction_artifact(self):
        self.plan_ui()
        self.fail(["dispatch", "U1", "--agent", "b1"], "direction artifact")
        (self.root / "DIRECTION.md").write_text("anchor + dials + ban list\n")
        self.ok(["direction", "DIRECTION.md"])
        self.ok(["dispatch", "U1", "--agent", "b1"])

    def test_ui_digest_requires_evidence_and_human_verify(self):
        self.plan_ui()
        (self.root / "DIRECTION.md").write_text("d\n")
        self.ok(["direction", "DIRECTION.md"])
        self.ok(["dispatch", "U1", "--agent", "b1"])
        self.ok(["run-acceptance", "U1"])
        rel = self.submit_ui_digest("U1", with_evidence=False)
        self.fail(["digest", "U1", rel], "evidence")
        rel = self.submit_ui_digest("U1", with_hv=False)
        self.fail(["digest", "U1", rel], "human_verify")

    def test_integration_holds_flags_hv_and_pick(self):
        self.plan_ui()
        (self.root / "DIRECTION.md").write_text("d\n")
        self.ok(["direction", "DIRECTION.md"])
        self.ok(["dispatch", "U1", "--agent", "b1"])
        self.ok(["run-acceptance", "U1"])
        rel = self.submit_ui_digest("U1", flags=["portal transitions downgraded to cuts"])
        self.ok(["digest", "U1", rel])
        self.ok(["gate", "U1", "critic", "pass"])
        self.ok(["gate", "U1", "verifier", "pass"])
        self.ok(["gate", "U1", "design-critic", "pass", "--reason", "matches anchor"])
        self.assertEqual(self.state_of("U1"), "done")

        self.fail(["integrate", "U1"], "unacknowledged flags")
        self.ok(["flag-ack", "U1", "--flag", "0", "--resolution", "escalated",
                 "--note", "quality downgrade goes to decision"])
        self.fail(["integrate", "U1"], "human-verify items pending")
        self.ok(["human-verify", "U1", "--item", "0", "--pass"])
        self.fail(["integrate", "U1"], "no recorded pairwise pick")
        self.ok(["pick", "U1", "--options", "variant-a|variant-b", "--choice", "variant-a"])
        self.ok(["integrate", "U1"])
        self.assertEqual(self.state_of("U1"), "integrated")

    def test_human_verify_fail_blocks_and_reopen_reworks(self):
        self.plan_ui()
        (self.root / "DIRECTION.md").write_text("d\n")
        self.ok(["direction", "DIRECTION.md"])
        self.ok(["dispatch", "U1", "--agent", "b1"])
        self.ok(["run-acceptance", "U1"])
        rel = self.submit_ui_digest("U1")
        self.ok(["digest", "U1", rel])
        self.ok(["gate", "U1", "critic", "pass"])
        self.ok(["gate", "U1", "verifier", "pass"])
        self.ok(["gate", "U1", "design-critic", "pass"])
        self.ok(["human-verify", "U1", "--item", "0", "--fail", "--note", "still frenetic"])
        self.fail(["integrate", "U1"], "FAILED")
        self.ok(["reopen", "U1", "--reason", "human verify failed on sound feel"])
        self.assertEqual(self.state_of("U1"), "dispatched")
        # Calibration telemetry records the critic-pass/human-fail disagreement.
        r = self.ok(["metrics"])
        m = json.loads(r.stdout)
        self.assertIn("U1", m["calibration"]["critic_pass_human_fail"])


class TestUsageTelemetry(HarnessBase):
    def setUp(self):
        super().setUp()
        self.ok(["init"])
        write_json(self.root / "plan.json", BASIC_PLAN)
        self.ok(["plan", "plan.json"])

    def test_usage_recorded_and_rolled_up(self):
        self.ok(["usage", "T1", "--tokens", "1200", "--duration-ms", "45000",
                 "--role", "builder", "--model", "sonnet", "--agent", "builder-1"])
        self.ok(["usage", "run", "--tokens", "800", "--duration-ms", "30000",
                 "--role", "orchestrator"])
        usage = [e for e in self.ledger_events() if e["ev"] == "USAGE"]
        self.assertEqual(len(usage), 2)
        self.assertEqual(usage[0]["task"], "T1")
        self.assertEqual(usage[0]["tokens"], 1200)
        self.assertEqual(usage[0]["duration_ms"], 45000)
        self.assertEqual(usage[0]["role"], "builder")
        self.assertEqual(usage[0]["model"], "sonnet")
        self.assertEqual(usage[0]["agent"], "builder-1")
        r = self.ok(["metrics"])
        m = json.loads(r.stdout)
        self.assertEqual(m["usage"]["total_tokens"], 2000)
        self.assertEqual(m["usage"]["tokens_by_role"],
                         {"builder": 1200, "orchestrator": 800})
        self.assertEqual(m["usage"]["tokens_by_task"], {"T1": 1200, "run": 800})
        self.assertAlmostEqual(m["usage"]["orchestrator_share"], 0.4)

    def test_unknown_role_rejected(self):
        self.fail(["usage", "T1", "--tokens", "1", "--duration-ms", "1",
                   "--role", "wizard"], "invalid choice")

    def test_unknown_task_rejected_run_exempt(self):
        self.fail(["usage", "ZZ", "--tokens", "1", "--duration-ms", "1",
                   "--role", "builder"], "not planned")
        self.ok(["usage", "run", "--tokens", "1", "--duration-ms", "1",
                 "--role", "orchestrator"])


class TestHypothesis(HarnessBase):
    def setUp(self):
        super().setUp()
        self.ok(["init"])
        write_json(self.root / "plan.json", BASIC_PLAN)
        self.ok(["plan", "plan.json"])

    def test_hypothesis_recorded(self):
        self.ok(["hypothesis", "T1", "--scheme", "solo-frontier",
                 "--verdict", "likely-worse", "--reason", "gates caught two real defects"])
        cf = [e for e in self.ledger_events() if e["ev"] == "COUNTERFACTUAL"]
        self.assertEqual(len(cf), 1)
        self.assertEqual(cf[0]["task"], "T1")
        self.assertEqual(cf[0]["scheme"], "solo-frontier")
        self.assertEqual(cf[0]["verdict"], "likely-worse")
        self.assertEqual(cf[0]["reason"], "gates caught two real defects")

    def test_bad_scheme_rejected(self):
        self.fail(["hypothesis", "T1", "--scheme", "voodoo",
                   "--verdict", "unclear", "--reason", "r"], "invalid choice")

    def test_bad_verdict_rejected(self):
        self.fail(["hypothesis", "T1", "--scheme", "other",
                   "--verdict", "maybe", "--reason", "r"], "invalid choice")

    def test_missing_or_empty_reason_rejected(self):
        self.fail(["hypothesis", "T1", "--scheme", "other", "--verdict", "unclear"])
        self.fail(["hypothesis", "T1", "--scheme", "other", "--verdict", "unclear",
                   "--reason", "   "], "non-empty")


class TestPark(HarnessBase):
    def setUp(self):
        super().setUp()
        self.ok(["init"])
        write_json(self.root / "plan.json", BASIC_PLAN)
        self.ok(["plan", "plan.json"])

    def test_banner_refusal_and_unpark(self):
        self.ok(["park", "--reason", "rate limit cluster"])
        r = self.ok(["status"])
        self.assertIn("PARKED since", r.stdout)
        self.assertIn("rate limit cluster", r.stdout)
        self.fail(["dispatch", "T1", "--agent", "builder-1"], "parked")
        self.ok(["unpark"])
        r = self.ok(["status"])
        self.assertNotIn("PARKED since", r.stdout)
        self.ok(["dispatch", "T1", "--agent", "builder-1"])
        self.assertEqual(self.state_of("T1"), "dispatched")

    def test_parked_check_precedes_ready_set_check(self):
        self.ok(["park", "--reason", "x"])
        r = self.fail(["dispatch", "T2", "--agent", "builder-1"], "parked")
        self.assertNotIn("ready set", r.stderr)

    def test_double_park_and_stray_unpark_rejected(self):
        self.fail(["unpark"], "not parked")
        self.ok(["park", "--reason", "x"])
        self.fail(["park", "--reason", "y"], "already parked")


class TestParkPrecedence(HarnessBase):
    def test_parked_check_precedes_genesis_gate(self):
        self.ok(["init", "--mode", "genesis"])
        write_json(self.root / "plan.json", {"tasks": [
            {"id": "G1", "goal": "skeleton", "deps": [], "acceptance": ["true"]},
        ]})
        self.ok(["plan", "plan.json"])
        self.ok(["park", "--reason", "x"])
        r = self.fail(["dispatch", "G1", "--agent", "builder-1"], "parked")
        self.assertNotIn("genesis", r.stderr)


class TestModelGuard(HarnessBase):
    def test_non_frontier_refused(self):
        self.fail(["init", "--orchestrator-model", "claude-sonnet-4-5"],
                  "--allow-non-frontier")
        self.assertFalse((self.root / ".harness").exists())

    def test_non_frontier_override_recorded(self):
        self.ok(["init", "--orchestrator-model", "claude-sonnet-4-5",
                 "--allow-non-frontier"])
        with open(self.root / ".harness" / "session.json") as f:
            header = json.load(f)
        self.assertEqual(header["orchestrator_model"], "claude-sonnet-4-5")
        self.assertTrue(header["non_frontier_ack"])
        init_ev = [e for e in self.ledger_events() if e["ev"] == "INIT"][0]
        self.assertEqual(init_ev["orchestrator_model"], "claude-sonnet-4-5")
        self.assertTrue(init_ev["non_frontier_ack"])

    def test_frontier_passes_through(self):
        self.ok(["init", "--orchestrator-model", "claude-fable-5"])
        with open(self.root / ".harness" / "session.json") as f:
            header = json.load(f)
        self.assertEqual(header["orchestrator_model"], "claude-fable-5")
        self.assertFalse(header["non_frontier_ack"])

    def test_frontier_match_is_case_insensitive_substring(self):
        self.ok(["init", "--orchestrator-model", "Claude-Opus-4-8"])


class TestPlanFields(HarnessBase):
    def setUp(self):
        super().setUp()
        self.ok(["init"])

    def plan(self, plan_obj):
        write_json(self.root / "plan.json", plan_obj)
        return harness(["plan", "plan.json"], self.root)

    def test_defaults_and_override_recorded(self):
        plan = {"defaults": {"builder_model": "opus"}, "tasks": [
            {"id": "A", "goal": "a", "deps": [], "acceptance": ["true"]},
            {"id": "B", "goal": "b", "deps": [], "acceptance": ["true"],
             "builder_model": "sonnet", "risk": "low"},
        ]}
        r = self.plan(plan)
        self.assertEqual(r.returncode, 0, r.stderr)
        events = self.ledger_events()
        wave = [e for e in events if e["ev"] == "WAVE_PLANNED"][0]
        self.assertEqual(wave["defaults"], {"builder_model": "opus"})
        planned = {e["task"]: e for e in events if e["ev"] == "PLANNED"}
        self.assertEqual(planned["A"]["builder_model"], "opus")
        self.assertEqual(planned["A"]["risk"], "standard")
        self.assertEqual(planned["B"]["builder_model"], "sonnet")
        self.assertEqual(planned["B"]["risk"], "low")

    def test_invalid_risk_rejected(self):
        plan = {"tasks": [{"id": "A", "goal": "a", "deps": [], "acceptance": ["true"],
                           "risk": "extreme"}]}
        r = self.plan(plan)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("risk", r.stderr)

    def test_ui_surface_requires_explicit_high_risk(self):
        base = {"id": "U9", "goal": "ui", "deps": [], "acceptance": ["true"],
                "ui_surface": True, "extra_gates": ["design-critic"]}
        r = self.plan({"tasks": [dict(base)]})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("risk high", r.stderr)
        r = self.plan({"tasks": [dict(base, risk="low")]})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("risk high", r.stderr)
        r = self.plan({"tasks": [dict(base, risk="high")]})
        self.assertEqual(r.returncode, 0, r.stderr)


class TestMetricsV3(HarnessBase):
    def setUp(self):
        super().setUp()
        self.ok(["init"])
        write_json(self.root / "plan.json", BASIC_PLAN)
        self.ok(["plan", "plan.json"])

    def test_sections_and_arithmetic(self):
        self.ok(["usage", "T1", "--tokens", "3000", "--duration-ms", "1000",
                 "--role", "builder"])
        self.ok(["usage", "run", "--tokens", "1000", "--duration-ms", "500",
                 "--role", "orchestrator"])
        self.ok(["hypothesis", "T1", "--scheme", "solo-frontier",
                 "--verdict", "likely-worse", "--reason", "r1"])
        self.ok(["hypothesis", "T2", "--scheme", "solo-frontier",
                 "--verdict", "likely-worse", "--reason", "r2"])
        self.ok(["hypothesis", "T1", "--scheme", "sonnet-builders",
                 "--verdict", "unclear", "--reason", "r3"])
        # Seeded park pair with controlled timestamps: 30 closed minutes,
        # plus an open park that must contribute zero.
        self.seed_event({"ev": "PARKED", "ts": "2026-07-18T10:00:00+0000", "reason": "429s"})
        self.seed_event({"ev": "UNPARKED", "ts": "2026-07-18T10:30:00+0000"})
        self.seed_event({"ev": "PARKED", "ts": "2026-07-18T11:00:00+0000", "reason": "still open"})
        r = self.ok(["metrics"])
        m = json.loads(r.stdout)
        self.assertEqual(m["usage"]["total_tokens"], 4000)
        self.assertEqual(m["usage"]["tokens_by_role"],
                         {"builder": 3000, "orchestrator": 1000})
        self.assertEqual(m["usage"]["tokens_by_task"], {"T1": 3000, "run": 1000})
        self.assertAlmostEqual(m["usage"]["orchestrator_share"], 0.25)
        self.assertEqual(m["hypotheses"],
                         {"solo-frontier": {"likely-worse": 2},
                          "sonnet-builders": {"unclear": 1}})
        self.assertEqual(m["parks"], {"count": 2, "parked_minutes": 30.0})

    def test_no_usage_means_null_share(self):
        r = self.ok(["metrics"])
        m = json.loads(r.stdout)
        self.assertEqual(m["usage"]["total_tokens"], 0)
        self.assertIsNone(m["usage"]["orchestrator_share"])
        self.assertEqual(m["parks"], {"count": 0, "parked_minutes": 0.0})


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
