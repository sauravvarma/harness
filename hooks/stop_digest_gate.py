#!/usr/bin/env python3
"""Stop-hook example: a harness builder may not finish without a valid digest.

Wire this as a Stop hook in the builder agent's settings (see
settings.example.json). The builder's spawn environment must set
HARNESS_TASK to its task id. The hook submits the digest through the CLI;
the CLI's validation (schema, report existence, acceptance evidence) decides
whether stopping is legal. The protocol enforces itself; prompts alone drift.

Note: `harness digest` records ledger events, so this hook is the intended
submission path; a builder that already submitted successfully exits cleanly
on the state check below.
"""

import json
import os
import shutil
import subprocess
import sys


def harness_bin():
    found = shutil.which("harness")
    if found:
        return found
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "harness")


def main():
    task = os.environ.get("HARNESS_TASK", "")
    if not task:
        return 0  # not a harness-managed agent; allow stop

    digest_path = os.path.join(".harness", "digests", "%s.json" % task)
    if not os.path.exists(digest_path):
        print(json.dumps({
            "decision": "block",
            "reason": "no digest at %s; write it and it will be validated" % digest_path,
        }))
        return 0

    # If the task already left the dispatched state, the digest was accepted.
    hb = harness_bin()
    status = subprocess.run([hb, "status"], capture_output=True, text=True)
    for line in status.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == task and parts[1] != "dispatched":
            return 0

    r = subprocess.run([hb, "digest", task, digest_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(json.dumps({
            "decision": "block",
            "reason": "digest rejected by harness: %s" % (r.stderr.strip() or r.stdout.strip()),
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
