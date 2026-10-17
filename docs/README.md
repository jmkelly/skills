# skills

Agent skills for the [pi](https://github.com/badlogic/pi) LLM harness and other [Agent Skills](https://agentskills.io) clients, installed at `~/.agents/skills` or your harness's skills folder.

[`kelso-mode`](../kelso-mode/SKILL.md) is the entry to the set. Trigger it with `kelso` or `/kelso-mode`. It routes each request to the skill that owns it, names the principles that shaped the result, and runs the matched playbook. Every other skill in the repo is bundled with it.

## Skills

| Skill | What it does | When to reach for it | License |
|---|---|---|---|
| [`kelso-mode`](../kelso-mode/SKILL.md) | Trigger routing, the principles catalog, subagent defaults, autonomy, reply and comment style, and beads-based playbook mechanics. | Asked to work in kelso mode. | MIT © James Kelly |
| [`quality-loop`](../quality-loop/README.md) | Deterministic quality gates for **.NET** and **Python** repos — CRAP < 10 per method/function, metrics (radon / Dependably.CodeMetrics), Stryker mutation testing — plus a two-agent loop (verifier + implementor) that drives the gates green. Stack auto-detected; gates exit 0/1, offenders land in `crap-queue.md` / `metrics-queue.md` / `stryker-queue.md`. | Asked to run the quality loop or an audit; a queue reports offenders; the gates or their tooling are mentioned. | MIT © James Kelly |
| [`writing-for-agents`](../writing-for-agents/SKILL.md) | Reference for writing documents agents consume: skills, `AGENTS.md` / `CLAUDE.md`, docs reached by a pointer. Covers context pointers, the two loads (context vs cognitive), and information hierarchy; see [`SKILL-MECHANICS.md`](../writing-for-agents/SKILL-MECHANICS.md) for skill packaging. Upstream: [mattpocock/skills](https://github.com/mattpocock/skills). | Creating or editing skills, or modifying `AGENTS.md` / `CLAUDE.md`. | MIT © Matt Pocock |

## Bundled leaves

`kelso-mode` bundles 29 leaf skills, each also invocable on its own:

- [`arena`](../arena/SKILL.md) runs N parallel candidates at one task, picks a base, and grafts the strongest parts of the losers into it.
- [`show-me-your-work`](../show-me-your-work/SKILL.md) keeps a reviewable decision trail for long or unattended runs.
- [`benchmark-checklist`](../benchmark-checklist/SKILL.md) vets a measured performance number before you report or act on it.
- [`automate-me`](../automate-me/SKILL.md) drafts or refreshes a personal `-mode` skill from the user's working conventions and recent transcripts.
- [`unslop`](../unslop/SKILL.md) cuts AI tells from any writing, and `automate-me` applies it to the draft.
- The 24 [`principle-*`](../kelso-mode/SKILL.md) skills hold one leaf per catalog principle, read when that principle applies.

All MIT © Lauren Tan.

## License

Per-component MIT, one `LICENSE` per component: © James Kelly for the repo, `kelso-mode`, and `quality-loop`; © Lauren Tan for the bundled leaves; © Matt Pocock for `writing-for-agents`.