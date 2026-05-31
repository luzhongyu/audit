# audit

An 8-stage vulnerability-discovery agent, powered by the **OpenCode Server API**.
Many narrow agents, deliberate disagreement, and an explicit reachability gate.

MIT-licensed. No API key needed if you already use `opencode`.

## Origin

This project is a from-scratch reimplementation of the pipeline described in
Cloudflare's [Project Glasswing](https://blog.cloudflare.com/cyber-frontier-models/)
post, which tested Anthropic's Mythos preview LLM against Cloudflare's own
codebase. The blog argues that real-world vulnerability discovery does **not**
come from asking one big model "find bugs here" — it comes from:

1. **Many narrow agents** working in parallel on tightly-scoped questions
   ("Look for command injection in this specific function, with this trust
   boundary above it") rather than one exhaustive agent.
2. **Deliberate disagreement** — a second agent, on a different model, that
   tries to *disprove* the first agent's findings.
3. **A reachability trace** as the gating step — most "is this code buggy?"
   findings are noise unless an attacker-controlled input can actually reach
   the sink from outside the system.
4. **A feedback loop** so reachable bugs in one place automatically seed
   hunts for the same pattern elsewhere.

This repo packages that pipeline into a runnable agent. The Cloudflare post
showed the architecture; this codebase ships the prompts, schemas, state
store, and orchestrator.

## The 8 stages

![Vulnerability discovery harness — 8 stages](https://raw.githubusercontent.com/evilsocket/audit/main/docs/pipeline.png)

<sub>Diagram from Cloudflare's [Project Glasswing](https://blog.cloudflare.com/cyber-frontier-models/) post, reproduced here for reference.</sub>

| # | Stage    | Purpose |
|---|----------|---------|
| 1 | Recon    | Map the repo, emit narrowly-scoped Hunt tasks |
| 2 | Hunt     | One attack class per agent; compile/run PoCs |
| 3 | Validate | Adversarial re-read; tries to **disprove** (different model from Hunt) |
| 4 | Gapfill  | Re-queue under-covered areas |
| 5 | Dedupe   | Cluster findings by root cause |
| 6 | Trace    | Prove attacker-controlled input reaches the sink |
| 7 | Feedback | Turn reachable traces into new Hunt tasks |
| 8 | Report   | Schema-validated structured report |

Each stage is one markdown prompt in `prompts/` + one JSON Schema in
`schemas/`. The orchestrator passes the schema into the system prompt so
every output is shape-stable on the first try.

## Quickstart

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Install OpenCode (if not already installed)
curl -fsSL https://opencode.ai/install | bash

# 3. Verify
audit auth-check

# 4. Run
audit run --repo /path/to/target --run-id my-run
audit status --run-id my-run
audit report --run-id my-run --format md > report.md
```

The runner auto-starts `opencode serve` in the background.
You can also start it manually: `opencode serve`.

OpenCode handles its own authentication — configure your provider via
`opencode.json` or the `/connect` TUI command.

## OpenCode integration

This project originally used the **Claude Code Agent SDK** (`claude-agent-sdk`)
to drive agent calls. It has been rewritten to use the
**[OpenCode Server HTTP API](https://opencode.ai/docs/server)** instead.

### Why the switch

| Concern | Claude Code Agent SDK | OpenCode Server API |
| --- | --- | --- |
| Provider lock-in | Anthropic API key required | Any provider OpenCode supports |
| Open-source | SDK is proprietary | OpenCode is MIT-licensed |
| Cost | Anthropic pricing only | Use DeepSeek, open-weight models, etc. |
| Permission handling | Interactive prompts block CI | `external_directory: allow` via config |
| Session model | One-shot, stateless | Long-lived sessions + repair turns |

### How it works

`audit/opencode_client.py` is a thin async HTTP client that talks to
`opencode serve` (default `http://127.0.0.1:4096`). Every agent call follows
this lifecycle:

1. **Ensure server is running** — `ensure_running()` checks `/global/health`;
    if the server is down, it auto-spawns `opencode serve` with
    `OPENCODE_CONFIG_CONTENT='{"permission":{"external_directory":"allow"}}'`
    so agents can read target repo files without interactive prompts.

2. **Create a short-lived session** — `POST /session?directory=/path/to/repo`
    with an `x-opencode-directory` header scopes the session to the target
    codebase. Each agent call gets its own session.

3. **Send the prompt** — `POST /session/{id}/message` with `parts`
    (user message), `system` (stage prompt + output schema), `model`, and
    `tools`. The server runs the tool-use loop automatically and returns the
    final assistant response.

4. **Schema-validate the output** — if the JSON doesn't match the stage's
    schema, a repair turn is sent on the same session, asking the model to
    fix only the schema errors. Up to `repair_attempts` retries.

5. **Delete the session** — `DELETE /session/{id}` cleans up.

6. **Retry on transient errors** — HTTP 5xx, server overload, quota
    exhaustion, and OpenCode internal race conditions are classified and
    retried with exponential backoff (configurable per stage).

The `x-opencode-directory` header on the HTTP client sets the working
directory for all requests. Session-scoped directory is passed as a query
param on `POST /session` so each agent sees the correct repo.

### Server lifecycle

The runner reuses an existing `opencode serve` process if one is already
running on the configured port. It only starts a new server when the health
check fails. Concurrent tasks are gated by an `asyncio.Lock` to prevent
racing to start the server.

## Configuring models

Per-stage model overrides are in `config/stages.yaml`:

```yaml
stages:
  recon:
    model: deepseek-v4-flash
    concurrency: 1
    tools: [Read, Grep, Glob, Bash]
  validate:
    model: deepseek-v4-pro        # different from Hunt — deliberate disagreement
    tools: [Read, Grep, Glob]
```

Models are resolved by the OpenCode server. Set your default model in
`opencode.json` or via environment variables (e.g. `OPENCODE_DEFAULT_MODEL`).

## Cost containment

A real production codebase can produce 15-50 Hunt tasks and 25+ findings to
validate. At default concurrency this gets expensive. Flags to keep it sane:

```bash
audit run --repo /path/to/target \
  --max-concurrency 1 \           # one agent call at a time
  --max-recon-tasks 15 \          # cap initial Hunt fanout
  --max-cost-usd 30               # abort cleanly if exceeded
```

The budget guard fires between *and* within stages — a per-task check in
Hunt cooperatively aborts rather than running 30 more tasks past the cap.

## Live-target reproduction (optional)

If the target has a running deployment, point the agents at it:

```bash
audit run --repo /path/to/target --run-id live \
  --max-concurrency 1 --max-cost-usd 30 \
  --target-url http://server.local:8888 \
  --target-creds email=admin@system.com \
  --target-creds password=changechangeme
```

## Scope notes (optional)

Pass target-specific scope rules / exclusions:

```bash
audit run --repo /path/to/target --scope-notes target_scope.md
```

## Layout

```
prompts/        8 stage prompts (markdown, loaded as system prompts)
schemas/        9 JSON schemas — every agent output is validated
config/         stages.yaml — model + concurrency + tool allowlist per stage
audit/          Python package
  auth.py       OpenCode CLI + server reachability check
  opencode_client.py  HTTP client for the OpenCode Server API (replaces Claude Code SDK)
  state.py      SQLite DAO (runs, tasks, findings, traces, dedupe, costs)
  runner.py     OpenCode API wrapper with schema validation + repair turn
  orchestrator.py pipeline driver
  stages/       one module per stage
work/           per-Hunt-task scratch dirs (sandbox for PoC compile/run)
results/        JSONL artifacts per stage + final report.json
state.db        SQLite (gitignored)
```

## Safety

Hunt agents have Bash and run inside per-task scratch dirs. They are **not**
sandboxed at the OS level. Run the audit inside a disposable VM or container
when you don't trust the target source — a target with malicious build
scripts could otherwise execute on your host during PoC compilation.

The agent reads everything you `--add-dir`, including any `.env` or
`secrets/` directories in the target. Outputs land in `results/<run-id>/`
which is `.gitignore`d but **not** scrubbed of those reads.

## License

[MIT](LICENSE). Reuse freely. No warranty.

## Acknowledgements

- The pipeline design is from Cloudflare's [Project Glasswing](https://blog.cloudflare.com/cyber-frontier-models/)
  blog post. The credit for the architecture goes there.
- Built on the [OpenCode Server API](https://opencode.ai/docs/server).
