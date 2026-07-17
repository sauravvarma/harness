# harness

The deterministic spine of a multi-agent orchestration system for Claude Code: a frontier-model orchestrator plans and governs; smaller delegatee agents execute; this CLI validates every state change so the mechanics hold regardless of what any model claims.

The model proposes; the machine disposes.

Design doc (v1.3, three adversarial review rounds): `~/vault/projects/orchestrator-harness/design.md`.

## What is enforced mechanically (tag: kernel)

- **Event-sourced ledger**, `.harness/ledger.jsonl`: append-only, this CLI is the sole writer, every event inherits the session header (operator, mode, version vector). Resume is replay.
- **Task state machine**: `planned -> dispatched -> in_gate -> done -> integrated -> deployed`, with `escalated` and gate-fail rework loops. Illegal transitions are hard errors, even when the orchestrator requests them.
- **Ready set** computed from the DAG, never inferred by a model. Plans are schema-validated; cycles rejected; every task must carry executable acceptance commands.
- **Gate floor** (non-removable): digest-valid, acceptance-ran, critic, verifier. The first two are machine-owned; `harness gate` refuses to record them by hand. Extra gates are add-only.
- **Acceptance as recorded evidence**: `harness run-acceptance` wraps execution; a done digest without passing acceptance evidence for the current attempt is rejected and the task returns to rework.
- **Typed escalations**: needs-decision, contract-conflict, scope-discovery, budget-exceeded. Escalated tasks cannot be re-dispatched until a decision is recorded.
- **Monotone metrics** (the generalized ratchet): registered with a direction (`down` for migration burndown, `up` for a growth corpus) and a counting command; every integration checks all of them and blocks on violation.
- **Contract versioning**: amendments bump versions and emit REVERIFY events for affected tasks (tier-aware: PROVISIONAL reverifies pre-integration tasks, FROZEN reverifies everything).
- **Structural refusals**: repo lease at init (single operator, loud failure); genesis mode blocks dispatch until a human-ratified charter and one passing corpus scenario exist; ui_surface tasks cannot dispatch without a registered direction artifact.
- **The taste layer (v0.2)**: taste is routed and enforced, never held, by the kernel. `ui_surface` tasks require the design-critic gate, rendered evidence in the digest, and human-verify items; `direction_adjacent` tasks additionally require a recorded pairwise pick (`harness pick`). Integration blocks on unacknowledged digest flags (`harness flag-ack`), unresolved human-verify items (`harness human-verify`), and missing picks. A human-verify FAIL routes to `harness reopen`. `harness metrics` reports critic-vs-human calibration disagreements so /improve can tune (or demote) the design-critic on evidence.
- **The governance layer (v0.3)**: `harness init` takes an optional `--orchestrator-model <name>`; init refuses unless the lowercased name contains `fable` or `opus`, or the operator passes `--allow-non-frontier`, and the choice is recorded on the session. `harness usage <task|run> --tokens N --duration-ms N --role <role> [--model <m>] [--agent <name>]` logs model spend per role, task, and agent, including the orchestrator's own usage. `harness hypothesis <task> --scheme <s> --verdict <v> --reason "..."` records the orchestrator's honest read on whether a cheaper scheme (`solo-frontier`, `frontier-builders`, `sonnet-builders`, `single-session`, or `other`) would have reached the same outcome for less. `harness park --reason "..."` and `harness unpark` gate dispatch during rate-limit clusters; `harness status` shows a `PARKED since <ts>: <reason>` banner while a park is open, and the parked check runs before the genesis and ready-set checks in `harness dispatch`. Plan tasks accept a per-task `builder_model` and `risk` (low, standard, high; `ui_surface` tasks must set `risk: high`), plus a plan-wide `defaults.builder_model` for staffing a swarm at one tier, with per-task values overriding the default.
- **Operator steering (v0.3, wave 2)**: `harness note "<text>"` appends an out-of-band operator note the orchestrator is expected to consume at every wake-up; `harness notes` lists them (seq and acked/unread), `harness notes --unread` filters to notes with no later ack, and `harness notes --ack <seq>` acknowledges one. This is how `! harness note "..."` reaches a busy run without waiting for the current turn to finish. `harness integrate --sha <v>` now validates that `<v>` is a full 40-character hex sha naming an existing git object (`git cat-file -e`) before writing anything; truncated or unresolvable shas are refused, and the no-sha default (current HEAD) is unchanged. `harness annotate <task-or-run> --text "..."` appends a sanctioned correction/observation to the ledger so no agent ever needs to hand-append an event.
- **Telemetry from the ledger**: `harness metrics` computes rework, escalation mix, gate catch rates, checkout count, and zero-catch (rubber-stamp) suspects, plus (v0.3) usage totals and orchestrator token share, hypothesis counts by scheme and verdict, and total parked minutes. Nothing is recalled; everything is derived.

## Install

Ships as a Claude Code plugin (this repo is its own marketplace):

```
claude plugin marketplace add sauravvarma/harness
claude plugin install harness@harness
```

That enables `/harness:conduct`, the roster agents (`harness:cartographer`, `harness:builder`, `harness:critic`, `harness:verifier`, `harness:integrator`), and the digest Stop-hook (inert unless `HARNESS_TASK` is set in an agent's environment).

The CLI is plain Python 3 stdlib with no install step. Inside Claude Code sessions the conduct skill resolves it via `${CLAUDE_PLUGIN_ROOT}/bin/harness`; for your own shell, symlink from a clone (the plugin cache path is version-suffixed, so link a clone, not the cache):

```
git clone git@github.com:sauravvarma/harness.git && ln -s "$PWD/harness/bin/harness" /usr/local/bin/harness
```

Update flow: bump `version` in `.claude-plugin/plugin.json` on every release, then `claude plugin update harness`. Validate before publishing: `claude plugin validate . --strict`.

## Layout

```
harness_cli/        the CLI (kernel; sole ledger writer)
bin/harness         entrypoint shim
.claude-plugin/     plugin + marketplace manifests
agents/             roster prompts (surface: versioned control surfaces)
skills/conduct/     the orchestrator's loop discipline
hooks/              digest Stop-hook + plugin hook wiring
templates/          packet template (content)
examples/           per-project settings wiring example
tests/              end-to-end tests through the real entrypoint
```

## Quick start

```
export PATH="$PATH:/path/to/harness/bin"
cd your-project
harness init --mode standard --task-type migration
harness plan plan.json
harness ready
harness dispatch T1 --agent builder-1
harness run-acceptance T1
harness digest T1 .harness/digests/T1.json
harness gate T1 critic pass --reason "conforms"
harness gate T1 verifier pass --reason "observed"
harness integrate T1
harness metrics
```

Tests: `python3 -m unittest discover -s tests`.

## Not yet built (in design, next increments)

- Worktree lifecycle automation (dispatch-time branching, mechanical pre-merge rebase, interaction-failure blame procedure): protocol specified in design v1.1, currently the integrator agent's manual discipline.
- Main-drift PAUSED/re-baseline (`harness rebase`).
- The /improve skill (capture exists via ledger, including calibration telemetry; distillation flow pending).
- Deterministic taste linters registered per project (slop detector, palette validator); motion-lint deliberately deferred until run evidence says which linter hurts most.
- Idempotent Stop-hook resubmission hardening.

Status: kernel v0.2 after two real runs (personal-site, aval) and a forensic post-mortem; the taste layer (HUMAN-VERIFY, flag triage, pairwise picks, design-critic, direction artifacts, calibration telemetry) is the direct product of those runs' failure classes.
