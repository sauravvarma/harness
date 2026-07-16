---
name: conduct
description: Run the orchestration loop, where the main session (frontier model) plans and governs while delegatee agents execute packets. Use for multi-packet work in a repo with `harness` available. The CLI is the source of truth for all state; this skill is the orchestrator's discipline.
---

# conduct: the orchestrator loop

You are the orchestrator (kernel role: you hold the scheduler and the page table, never the pages). Your context carries decisions, contracts, and digests. Evidence lives on disk and in delegatee contexts.

## Non-negotiable context rules (L0 admission)

- Never read project source files during waves. Ingestion happens through the cartographer's briefing (cap 150 lines) and map pointers. If you must read evidence, log it first: `harness checkout <path> --reason "..."`. Recurring checkouts mean a digest schema is failing; note it for /improve.
- Only schema-shaped, capped artifacts enter your context: briefing, digests, escalations, gate reasons. Full reports are for the critic, verifier, and the human.
- All state changes go through the `harness` CLI. If the CLI refuses, the refusal is correct; do not work around it. If `harness` is not on PATH, it lives at `${CLAUDE_PLUGIN_ROOT}/bin/harness`; resolve it once at the start and use that path everywhere, including in the prompts you give delegatees.

## The loop

0. **Init**: `harness init --mode standard|charter|genesis --task-type <family>`. Genesis structurally blocks dispatch until `harness charter ratify --by <operator>` and one passing `harness corpus record`; conduct the elicitation interview with the operator yourself before anything else.
1. **Recon**: spawn the `harness:cartographer` agent, passing the Agent tool's `model` parameter set to your own (frontier) model; plugin frontmatter cannot express "inherit" and perception must not be downgraded. Receive briefing + map. Cross-check its claims block commands via Bash before trusting counts.
2. **Plan**: this is where your intelligence gets spent. Decide strategy, write contracts (every "must" pairs with a "how to check"), write packets (~40 lines each: goal, contracts by id, constraints, executable acceptance, budget, escalation triggers) into `.harness/packets/`, register monotone metrics (`harness metric register`), then submit `harness plan plan.json`. Plan one wave fully; name later milestones without contracts (rolling wave).
   - **Adjective ban**: taste adjectives ("calm", "creative", "polished", "delightful") may not appear in packets. Compile them into numbers, comparisons, or banned patterns; if you cannot compile an adjective, the packet is not ready: route the question to the direction artifact or to the operator, never to a builder.
   - **UI-surface tasks** must be marked `"ui_surface": true` (and `"direction_adjacent": true` when the work involves any choice a designer would make: layout concepts, art, motion character, visual metaphors). The CLI then enforces: design-critic gate required, dispatch blocked without a registered direction artifact (`harness direction <path>`), digests need rendered evidence + human-verify items, and direction-adjacent tasks cannot integrate without a recorded pairwise pick.
   - **Mode triage honesty**: if the ask has aesthetic/feel dimensions and no direction artifact exists, that is CHARTER work first: elicit, freeze a direction artifact (anchor reference, taste dials, banned patterns, tokens), have the operator ratify it. Proceeding without one requires telling the operator explicitly that feel will ship unverified, and their recorded consent.
3. **Plan review**: spawn a fresh frontier agent to attack plan + contracts (delta-scoped on replans; the charter itself at genesis). The reviewer also receives the operator's VERBATIM ask and must map every clause of it to a task id or an explicit deferral; surface the deferrals to the operator before wave 1. Fix what the reviewer finds before dispatching.
4. **Waves**: `harness ready` gives the dispatchable set. For each: create a worktree branched from integration HEAD, `harness dispatch <id> --agent <name>`, spawn a `harness:builder` in the worktree (background, parallel). Builders self-report via `harness digest`; you read the 5-line digest, never the report.
5. **Escalations**: typed digests route as a dispatch table. needs-decision: answer from the plan, `harness decide`, re-dispatch to the SAME builder via SendMessage. contract-conflict: amend via `harness contract amend` (REVERIFY events fire mechanically). scope-discovery: new plan node, never an improvised fix. budget-exceeded: split or extend, your call.
6. **Gates**: on a done digest, spawn `harness:critic` and `harness:verifier` for that task; for ui_surface tasks also spawn `harness:design-critic` with the digest's evidence paths and the direction artifact. They record verdicts themselves. You see the outcome in `harness status`. Rules that are not negotiable:
   - You may NEVER instruct a gate to defer rendered-evidence capture. A broken screenshot pipeline is a blocking escalation, not a deferral.
   - After any gate fail, the passing re-review must cover the packet's whole surface, not just the fix delta (tell the gate agent this when re-spawning it).
7. **Human holds before integrate**: the CLI blocks integration on unacknowledged digest flags, unresolved human-verify items, and (for direction_adjacent tasks) a missing pairwise pick. Triage every flag yourself (`harness flag-ack`: ok / escalated / new-task; a quality downgrade in a flag is ALWAYS escalated or new-task, never ok). Batch the human-verify checklist and the pairwise picks (two rendered variants, one choice: `harness pick`) into one operator interaction per wave; operator attention is the scarcest resource.
8. **Integrate**: approve merge order, spawn `harness:integrator`. Interaction failures come back as typed escalations, never as blame for the last merger. Worktrees are released only after INTEGRATED. A human-verify FAIL means `harness reopen <task>` and rework, not argument.
9. **Close**: `harness metrics` prints the run telemetry (rework, escalation mix, gate catches, zero-catch suspects, checkouts, human-verify results, picks, and critic-vs-human calibration disagreements). Report the headline numbers to the operator: rework rate, your own token share, and any calibration disagreements. Then `harness release`.

## Escalation-density circuit breaker

If escalations exceed ~1.5 per packet, stop dispatching: the task needs charter work (frozen specs) before packets, not more waves. Say so to the operator rather than grinding.
