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
- **Structural refusals**: repo lease at init (single operator, loud failure); genesis mode blocks dispatch until a human-ratified charter and one passing corpus scenario exist.
- **Telemetry from the ledger**: `harness metrics` computes rework, escalation mix, gate catch rates, checkout count, and zero-catch (rubber-stamp) suspects. Nothing is recalled; everything is derived.

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
- HUMAN-VERIFY gate type as first-class events.
- The /improve skill (capture exists via ledger; distillation flow pending).
- Idempotent Stop-hook resubmission hardening.

Status: kernel v0 at N=0 real runs. The next milestone is one real STANDARD-mode run; per the council ruling, that outranks everything else.
