# skills

Agent skills for the [pi](https://github.com/badlogic/pi) LLM harness, as installed at `~/.pi/agent/skills`.

## Skills

| Skill | What it does | When to reach for it | License |
|---|---|---|---|
| [`quality-loop`](quality-loop/README.md) | Deterministic quality gates for **.NET** and **Python** repos — CRAP < 10 per method/function, metrics (radon / Dependably.CodeMetrics), Stryker mutation testing — plus a two-agent loop (verifier + implementor) that drives the gates green. Stack auto-detected; gates exit 0/1, offenders land in `crap-queue.md` / `metrics-queue.md` / `stryker-queue.md`. | Asked to run the quality loop or an audit; a queue reports offenders; the gates or their tooling are mentioned. | MIT © James Kelly |
| [`writing-for-agents`](writing-for-agents/SKILL.md) | Reference for writing documents agents consume: skills, `AGENTS.md` / `CLAUDE.md`, docs reached by a pointer. Covers context pointers, the two loads (context vs cognitive), and information hierarchy; see [`SKILL-MECHANICS.md`](writing-for-agents/SKILL-MECHANICS.md) for skill packaging. Upstream: [mattpocock/skills](https://github.com/mattpocock/skills). | Creating or editing skills, or modifying `AGENTS.md` / `CLAUDE.md`. | MIT © Matt Pocock |

## License

Per-component MIT — see `LICENSE` (repo, © James Kelly), `quality-loop/LICENSE`, and `writing-for-agents/LICENSE`.