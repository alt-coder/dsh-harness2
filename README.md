# dsh-harness2

Run [Harness²](https://github.com/google-research/harness2) (Google Research's
recursive agent-harnessing loop) inside
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`).

You give it a **harness** (an `AGENTS.md` plus optional skills, scripts, and
subagents) and one or more **tasks**. Harness² runs the task with real dsh
sessions, proposes edits to the harness, probes them on the same task, and
composes a refined harness from what actually changed the agent's behaviour. No
grader or ground truth is needed. Every rollout and every editor session is a
`dsh --profile headless` run.

The repository contains two pieces that are installed separately:

| Piece | What it is | Installed with |
|---|---|---|
| `dsh-plugin-harness2` | A dsh plugin that adds three tools to the agent: `harness2_improve`, `harness2_result`, `harness2_cancel` | dsh's **Add plugin** dialog |
| `harness2-dsh` | The Python adapter and CLI that drive Harness² with dsh as the agent | `uv tool install` |

## Requirements

- Linux or macOS. Harness² itself is POSIX-only.
- [dsh](https://github.com/deepseek-ai/deepseek-harness) 0.2.1 or newer (Node.js 20+).
- [uv](https://docs.astral.sh/uv/getting-started/installation/). It fetches
  Python 3.12 or 3.13 for you if needed.
- An **API key** for the model the sweeps use, such as `DEEPSEEK_API_KEY`.
  Headless dsh runs cannot use the DeepSeek account sign-in.

## Install

**1. Install the adapter.** It pulls in Harness² from its GitHub repository.

```bash
uv tool install git+https://github.com/alt-coder/dsh-harness2
```

This puts `harness2-dsh` in `~/.local/bin`, where the plugin finds it.

**2. Add the plugin to dsh.** Open **Settings → Plugins → Add plugin** and enter:

```
github:alt-coder/dsh-harness2
```

(`https://github.com/alt-coder/dsh-harness2` works too.) If dsh reports
`restart-required`, restart it. You can also ask the dsh agent itself to *"install the plugin
github:alt-coder/dsh-harness2"*; it uses the same Plugin Manager.

**3. Choose the sweep model and check the setup.**

```bash
export DEEPSEEK_API_KEY=sk-...          # or store it on the dsh web Models page
harness2-dsh doctor --provider deepseek-official --model deepseek-flash
```

`doctor` checks Python, Harness², the dsh launcher, and a real one-line headless
session, and says what to fix if any of them fail. To make that model the default
for sweeps started from dsh, export these before starting dsh:

```bash
export HARNESS2_DSH_PROVIDER=deepseek-official
export HARNESS2_DSH_MODEL=deepseek-flash      # or deepseek-v4-pro
```

Without them, sweeps use whatever model your `headless` dsh profile is configured
with.

## Use it from dsh

Ask in a dsh session, for example:

> Refine the harness in `./harness` against the tasks in `./tasks/invoice` and
> `./tasks/ledger` with Harness2, k=1, then show me what it adopted.

The agent calls the tools:

| Tool | What it does |
|---|---|
| `harness2_improve` | Starts a sweep in the background and returns its run directory at once. Inputs: `harness`, `tasks` (one or more, run in order), and optionally `k` (task-level recursion budget, default 1), `mode` (`parallel`, `sequential`, or `both`), `out`, `dry_run`. Relative paths resolve against the session's workspace. |
| `harness2_result` | Reports `running`, `done`, `failed`, or `cancelled`. When the sweep is done, `adopted` maps each task and mode to the harness Harness² selected. |
| `harness2_cancel` | Stops a running sweep and every dsh session it started. |

Sweep state is stored on disk in the run directory, so you can still poll or
cancel a sweep after restarting dsh.

## Use it from the command line

```bash
harness2-dsh run --harness ./harness --task ./tasks/ledger --k 1
harness2-dsh run --harness ./harness --task ./tasks/a --task ./tasks/b --mode both --out ./runs/ab
harness2-dsh run --harness ./harness --task ./tasks/ledger --dry-run
```

Try it on the bundled example:

```bash
git clone https://github.com/alt-coder/dsh-harness2 && cd dsh-harness2
harness2-dsh run --harness examples/harness --task examples/tasks/ledger --out runs/ledger
```

## Harness and task layout

```
harness/                 task/
  AGENTS.md  (required)    task.md   (required: the prompt)
  .agents/skills/          any files the work reads
  scripts/
  sub_agents/
```

A task's directory name is its task id, so the tasks in one sweep need distinct
names. The agent is asked to write its final answer to `answer.md`.

## What a run produces

```
<run>/
  summary.json            plan, done/failed counts, adopted paths
  adopted/<task>/<mode>/  the harness Harness² selected: copy this one
  runs/                   every rollout: workspace, events.ndjson, trajectory.md, answer.md
  proposer/               every editor session's mission and response
  library/                lessons carried from earlier tasks to later ones
  run.log, pid, exit_code
```

`mode` is `agg` for parallel recursion and `seq` for sequential recursion.

## How the two recursion levels map

- **Task-level recursion** (Propose–Probe–Compose) runs on every task. `k` sets
  its budget and `mode` sets parallel or sequential recursion.
- **Harnessing-level recursion** runs across the tasks of a single sweep: each
  task is refined with the harnesses and lessons adopted on the earlier ones.
  Harness² keeps the library only for the tasks of the current stream, so put
  related tasks in **one** sweep to have lessons carry forward.

## Configuration

Every setting is optional. To override one, add a row to your profile's
`cordis.patch.yml` (in `~/.dsh/profiles/<profile>/`):

```yaml
- id: harness2-tools
  name: dsh-plugin-harness2
  config:
    executable: /opt/venvs/h2/bin/harness2-dsh   # default: PATH, then ~/.local/bin
    defaultOutDir: harness2-runs                # relative to the session workspace
    provider: deepseek-official                 # with model: the model for every sweep session
    model: deepseek-flash
    dshBin: /usr/local/bin/dsh                  # default: the dsh running the plugin
```

| Environment variable | Read by | Meaning |
|---|---|---|
| `HARNESS2_DSH_PROVIDER`, `HARNESS2_DSH_MODEL` | `harness2-dsh` | Model for every rollout and editor session |
| `HARNESS2_DSH_BIN` | `harness2-dsh` | dsh launcher. Default: `dsh` on PATH, then `npx @deepseek-ai/dsh` |
| `HARNESS2_DSH_PROFILE` | `harness2-dsh` | dsh profile for sessions (default `headless`) |
| `HARNESS2_DSH_EXECUTABLE` | plugin | Path to `harness2-dsh` |
| `DEEPSEEK_API_KEY`, `OPENCODE_GO_API_KEY`, `ANTHROPIC_API_KEY` | `harness2-dsh` | Passed to headless sessions. If unset, the adapter falls back to the refs dsh stores in `~/.dsh/.credentials.yaml`. |

## Cost

A sweep with `k=1` on one task runs about 4 rollouts and 3 editor sessions per
mode. `mode: both` doubles that, and each additional task or step of `k` adds
more. Start with one small task and `dry_run` to see the plan first.

## Troubleshooting

- **Every dsh message fails with `Invalid schema for function ...`.** An
  installed plugin declares an invalid tool schema. Turn it off in
  **Settings → Plugins**. This plugin's schemas are tested against dsh's own
  validator.
- **`MISSING_CREDENTIAL` or `ACCOUNT_SIGN_IN_REQUIRED` in `doctor` or `run.log`.**
  The headless model has no usable key. See install step 3.
- **`harness2-dsh is not installed`.** Run install step 1, or set
  `config.executable`.

## Development

```bash
uv venv --python 3.12 && uv pip install -e .
.venv/bin/python -m unittest discover -s tests -v
npm install --no-save @deepseek-ai/dsh-tools@0.2.1-rc.2 && npm test
```

To try a local checkout in dsh, enter the checkout's absolute path in
**Add plugin** instead of the GitHub address.

## License

Apache-2.0. Harness² is by Google Research and is installed from
[google-research/harness2](https://github.com/google-research/harness2) under its
own Apache-2.0 license. This project is not affiliated with Google or DeepSeek.
