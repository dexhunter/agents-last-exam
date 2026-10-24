# OpenClaw CLI Runner — Integration Notes

Standalone deployer that drives OpenClaw via an episode-local Gateway.
Implements `BaseAgentDeployer` directly (no inheritance chain) with three
entry points: `install()`, `launch()`, `parse_artifacts()`.

## Source

| Aspect | Value |
|---|---|
| Source | Pinned official npm package in a private prefix; optional custom tarball must match `cli_version`. |
| CLI subcommand | `openclaw agent --agent main --session-id <uuid> --message-file prompt.txt --json --timeout N --thinking high` |
| Output channel | stdout or stderr |
| Plugin loading | Process-startup-tied; `<state>/extensions/cua/dist/index.cjs` |
| CUA bridge | Native OpenClaw plugin (LiteDesktopActionSpace), NOT MCP |

## Deployer Lifecycle

`OpenClawCliDeployer` extends `BaseAgentDeployer` directly — there is no
intermediate `OpenClawDeployer` or `ExternalAgentDeployer` base class.
Everything runs locally inside the sandbox.

| Method | Purpose |
|---|---|
| `install()` | Install and verify the CLI and Node, build the CUA plugin, write config, migrate credentials, and verify plugin tools |
| `launch(prompt)` | Start the Gateway and agent, wait for subagents, export session trajectories, and clean up processes |
| `parse_artifacts(work_dir, ...)` | Parse session `transcript.jsonl` and the result envelope into trajectory steps |

## CLI Invocation

```bash
openclaw gateway run --bind loopback --port "$PORT"
NO_COLOR=1 openclaw agent \
  --agent main \
  --session-id "$SESSION_ID" \
  --message-file prompt.txt \
  --json \
  --timeout 600 \
  --thinking high
```

The Gateway uses a fresh token and loopback port per episode, without messaging
channels. The deployer supervises both processes on Linux and Windows, waits for
child delivery and requester continuation, then cleans up the process tree.

## --json Envelope Shape

```json
{
  "payloads": [{"text": "...", "mediaUrl": null}],
  "meta": {
    "durationMs": 33958,
    "agentMeta": {
      "sessionId": "<uuid>",
      "provider": "openrouter",
      "model": "openai/gpt-5.4",
      "usage": {"input": 19345, "output": 5, "total": 19350},
      "lastCallUsage": {"input": N, "output": N, "cacheRead": N, "cacheWrite": N}
    },
    "finalAssistantVisibleText": "...",
    "stopReason": "stop",
    "executionTrace": {
      "winnerProvider": "openrouter",
      "winnerModel": "openai/gpt-5.4",
      "runner": "embedded"
    }
  }
}
```

Stderr preamble lines (stripped by `_parse_stderr_json`):
- `[agent/embedded] session file repaired: ...`
- `[agent/embedded] embedded run agent end: ...`
- `[diagnostic] lane task error: ...`
- `[model-fallback/decision] ...`

## Install Details

### 1. CLI tarball

`install()` creates a private state directory per episode, installs and verifies the official npm release at `cli_version`
in a private prefix. It installs a compatible private Node when necessary.
An optional tarball path or URL overrides the package source, but its version
must match `cli_version`.

### 2. CUA plugin build

If `~/.openclaw/extensions/cua/dist/index.cjs` does not exist:
1. Look for plugin source at `config.cua_plugin_path` (default: `/opt/ale/openclaw-cua-plugin`)
2. If the local path does not exist and `config.cua_plugin_repo` is set, sparse-clone the plugin from the repo (branch `config.cua_plugin_branch`, default `agenthle`)
3. Build: `npm install --no-audit --no-fund` then `npm run build` (uses esbuild under the hood)
4. Cache the built artifacts under `~/.openclaw/extensions/cua/`, then copy them into the episode state directory

### 3. Config write

Write the config and workspace files, migrate auth profiles to SQLite with `openclaw doctor
--fix --non-interactive`, and verify that all 14 CUA tools load.

### 4. Pre-warm the plugin runtime-deps mirror

When `openclaw` is installed to a global npm prefix (Windows
`AppData\Roaming\npm`, or `~/.local` on Linux) it cannot write its bundled-plugin
runtime deps in place, so on first plugin-load it **mirrors the bundled plugins'
dist tree** (~60 plugins, thousands of files) into
`<state>/plugin-runtime-deps/openclaw-<ver>-<hash>/dist/extensions/` under a
filesystem lock. On Windows this recursive copy takes **minutes**. If it runs
inside the timed `agent` turn it emits no stdout/stderr until done and
blows the agent's wall budget — the silent Windows runtime hang.

The mirror is built once and reused (existing targets are skipped). `install()`
runs a cheap `openclaw models list` (loads the same provider plugins) to pay this
cost up front during the untimed install, so every subsequent `launch()` is fast.
Best-effort: failure is non-fatal (the agent turn would just pay the cost
itself). See `_prewarm_plugin_runtime_mirror()`.

## Config File Layout

Written by `_write_config()` during `install()`:

### `<state>/openclaw.json`

Agent model, timeout, plugin allow/deny, tool allow/deny, gateway mode
(`local`), heartbeat, vision model.

### `<state>/agents/main/agent/auth-profiles.json`

Legacy migration source for provider credentials. OpenClaw imports these into
`<state>/agents/main/agent/openclaw-agent.sqlite`.

### `<state>/workspace/.openclaw/workspace-state.json`

Marks setup as completed (`setupCompletedAt`) to skip the bootstrap wizard.
**Must live under the agent *workspace* (`<state>/workspace/`), not
`<state>/state/`** — OpenClaw's agent resolves bootstrap
state from the workspace copy. `_complete_workspace_bootstrap()` writes this,
removes `<state>/workspace/BOOTSTRAP.md`, and seeds `MEMORY.md` +
`memory/{today,yesterday}.md`. The runtime re-materializes the workspace (with a
fresh `BOOTSTRAP.md`) lazily on first run, so `launch()` re-asserts these markers
right before spawning the agent.

If bootstrap stays *pending*, `agent` enters the interactive "who am I?"
bootstrap conversation instead of the task. Under `--json` (which buffers all
output until the turn ends) this produces **zero stdout/stderr and never exits**
on a headless VM — the silent Windows runtime hang. (The earlier deployer wrote
`workspace-state.json` to the wrong path, `~/.openclaw/state/`, and deleted the
wrong `BOOTSTRAP.md`, so the skip never took effect.)

## Config Fields

| Field | Meaning |
|---|---|
| `cli_version` | Exact official npm release, default `2026.10.5` |
| `tarball_path` | Optional custom package tarball inside the sandbox |
| `tarball_url` | Optional package URL when `tarball_path` is empty or missing |
| `llm_idle_timeout_s` | Optional provider request and LLM idle timeout in seconds; requires `base_url` |
| `cua_plugin_path` | Path to CUA plugin source directory (default: `/opt/ale/openclaw-cua-plugin`) |
| `cua_plugin_repo` | Git URL to clone CUA plugin source from when `cua_plugin_path` is missing (default: `https://github.com/cua-verse/openclaw.git`) |
| `cua_plugin_branch` | Branch to clone for CUA plugin source (default: `agenthle`) |
| `model` | OpenRouter model slug (e.g. `openai/gpt-5.4`) |
| `thinking` | Reasoning depth: `off\|low\|medium\|high` |
| `vision_model` | Per-tool model override for image analysis |
| `tools_deny` | Tools removed from the schema |
| `plugins_allow` | Optional plugin allowlist; empty uses upstream defaults |
| `heartbeat_every` | `null` uses the default cadence; `"never"` disables recurring heartbeats |

## Tool Classification

### CUA Plugin Tools (14 — native, not MCP)

`screenshot`, `click`, `type`, `key`, `key_down`, `key_up`, `hold_key`,
`mouse_move`, `mouse_down`, `mouse_up`, `drag`, `scroll`, `wait`,
`cursor_position`.

The plugin registers desktop tools with `executionMode: "sequential"` to preserve
order. Tool discovery, session tools, memory search, and subagent policy use
OpenClaw defaults. No browser, messaging channel, or embedding provider is
configured. Memory is episode-local; semantic recall requires an embedding
provider, and may otherwise fall back to keyword search.

The Gateway supports subagent delivery during the episode. Its default heartbeat
and cron jobs exist only while the episode is running; ALE does not persist or
schedule these jobs after the Gateway is stopped. Gateway-owned background
`exec` processes also end at shutdown; the agent must collect required results
before finishing.

## Session Trajectory

Current sessions live in `<state>/agents/main/agent/openclaw-agent.sqlite`.
The deployer exports ordered transcript events to JSONL, including compressed
events and child sessions. Legacy per-session JSONL files contain message events with `content[]` blocks:
- `type: "message"` with `message.role` + `message.content[]`
  (text, thinking, toolCall, tool_result blocks)
- `type: "tool_result"` with `output`, `call_id`, `is_error`

`parse_artifacts()` reads `transcript.jsonl` and the result envelope from
stdout or stderr to populate the trajectory.

## Artifacts

`parse_artifacts()` reads:

```
<work_dir>/
├── transcript.jsonl    # session events exported from SQLite or legacy JSONL
├── subagents/          # child transcripts and session relationship index
├── gateway.log         # Gateway output
├── stderr.log          # contains the --json envelope + diagnostic preamble
├── stdout.log          # may contain the --json envelope
└── prompt.txt          # the prompt sent to the agent
```

The method populates the `TrajectoryBuilder` with:
- One step per session event (assistant messages, tool calls, tool results)
- The full `--json` result envelope stored in `trajectory.extra["openclaw_cli"]`
- Usage/token counts from both the envelope and the session JSONL
