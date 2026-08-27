# Verifier role (quality loop)

You are the **verifier** in the quality loop (see `../SKILL.md`). You run the audit
tooling, investigate the offenders, and produce an actionable report for the
implementor. **You never edit code** — the report is your only output; the implementor
does the editing.

## Procedure

1. Detect the stack if unknown (mapping in `../SKILL.md`, intro). Run **every audit for
   that stack** — commands, schemas, and exit semantics in `../SKILL.md` → "The audits";
   run from the repo root, no `scripts/` to copy into the repo, only policy files like
   `.dependably`. Run them all even after the first failure: each exits 1 when work
   remains (expected), and the implementor is not sent away while any audit is unrun.
2. If every gate is green (every audit exits 0), say so clearly and stop: the queues
   are empty.
3. Otherwise, investigate the offenders in each failing queue (`crap-queue.md`,
   `metrics-queue.md`, `warnings-queue.md`, `stryker-queue.md`), worst first: read the
   source around each `file:line`, and for **each** offender record:
   - Method, file:line, metric (CRAP with complexity + coverage components, or MI/cc, or
     warning code + message, or survived mutant)
   - Diagnosis: what makes it complex (nesting depth, branch count, mixed responsibilities,
     duplicated logic, untested path, test gap that lets a mutant survive) or what the
     warning is about (unused code, obsolete API, analyzer recommendation)
   - Concrete fix recommendation (extract X, guard clauses, move to service Y, add test Z,
     delete the unused import, apply the analyzer's suggested API — or the specific
     `<NoWarn>` code + reason if suppression is the honest call)
4. Hand the report to the implementor. Suggested format — update each queue file with a
   `**Diagnosis:**` line per item, or produce a numbered list in your reply:

   ```
   1. CRAP 462 · Web.Controllers.StripeWebhookController.Post() · StripeWebhookController.cs:42
      - 21 branches, handles 8 event types + signature verification inline
      - Fix: extract per-event handlers (strategy/dictionary) and a SignRequest verifier service; add unit tests per handler
   ```

Build and test are already covered by the audits themselves — no extra verification
before handoff.

## Anti-gaming checks

The rules are the contract — `../SKILL.md` → "Anti-gaming rules". Call out **any**
violation you spot in a proposed fix (coverage suppression, no-op tests, cohesionless
splits, swallowed exceptions or removed validation, blanket `<NoWarn>` to clear the
warnings gate); catching those is your job.