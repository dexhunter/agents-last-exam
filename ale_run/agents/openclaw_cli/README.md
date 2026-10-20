# OpenClaw CLI Agent

Standalone deployer for `openclaw agent`, using the official npm package
and native CUA plugin (not MCP). Implements `BaseAgentDeployer` directly
with `install()` / `launch()` / `parse_artifacts()`.

```text
openclaw gateway run --bind loopback --port "$PORT"
  -> openclaw agent --agent main --session-id "$SESSION_ID" \
               --message-file prompt.txt --json --timeout N --thinking high
  -> OpenClaw built-in tools (read/write/exec/...)
  -> CUA plugin tools (LiteDesktopActionSpace)
     from <state>/extensions/cua/
  -> CUA Computer Server :8000
  -> desktop / filesystem
```

Each episode uses a fresh state directory, session ID, and loopback-only gateway
with a generated token. No messaging channel is configured. The gateway runs
only for the episode, and the deployer waits for spawned subagents and their
requester continuation before collecting transcripts and stopping it.

## Architecture

OpenClaw is installed at the pinned `cli_version` from npm by default. The CUA
bridge is the native OpenClaw plugin (not MCP), built from source at
install time via `npm install` + esbuild (`npm run build`).

| | Path |
|---|---|
| Binary | `~/.ale-openclaw/cli/<version>/node_modules/openclaw/openclaw.mjs` |
| Episode state | `~/.ale-openclaw/states/<uuid>/` |
| CUA plugin | `<state>/extensions/cua/dist/index.cjs` |
| Auth profiles and sessions | `<state>/agents/main/agent/openclaw-agent.sqlite` |

## Provider Routing

Auth profiles are migrated into SQLite by `openclaw doctor --fix`. Routing is set
by the `provider` config field (not inferred from which keys are in the
env):

- `provider: openrouter` (default) — openrouter auth profile keyed by
  `OPENROUTER_API_KEY`; the model is prefixed `openrouter/<model>`.
- `provider: direct` — the native provider is chosen by the model's
  vendor: an OpenAI model (`gpt-*` / `openai/...`) uses the `openai`
  provider + `OPENAI_API_KEY`; an Anthropic model (`claude-*` /
  `anthropic/...`) uses the `anthropic` provider + `ANTHROPIC_API_KEY`.
  `OPENROUTER_API_KEY` is dropped from the launch env so it can't override
  the chosen direct provider.
- `provider: zai` — the native Z.AI provider uses `ZAI_API_KEY` or
  `Z_AI_API_KEY`. Set `base_url` to select the global, CN, or coding endpoint.
- `provider: custom` — registers an OpenAI-compatible endpoint under
  `provider_id`. `model_id` is the provider-facing deployment name, while the
  top-level `model` remains the human-facing experiment label. The key is read
  from `api_key_env`.

`vision_provider` can route screenshot analysis independently from the
primary model. For example, a text-only Z.AI model can use
`vision_provider: direct` with `vision_model: openai/gpt-5.4`.

Missing the required key for the chosen provider is a hard error.

## Install

Three-stage install in `install()`:

1. **CLI**: Install the pinned official npm release in a private prefix and
   verify its version. If needed, install a compatible private Node runtime.
   `tarball_path` or `tarball_url` may select a custom package, which must
   match `cli_version`.
2. **CUA plugin**: Build from source at `config.cua_plugin_path`
   (default: `/opt/ale/openclaw-cua-plugin`). If the local source is
   missing and `config.cua_plugin_repo` is set, sparse-clone from the
   repo (branch `config.cua_plugin_branch`). Build via
   `npm install && npm run build` (esbuild). Artifacts installed:
   `package.json`, `openclaw.plugin.json`, `dist/index.cjs`.
3. **Config**: Write `openclaw.json`, `auth-profiles.json`, and
   `workspace-state.json`; migrate credentials with
   `openclaw doctor --fix` and verify all 14 CUA tools load.

The optional tarball and plugin source can be baked into the sandbox image,
volume-mounted, or fetched at install time.

The CUA plugin supplies desktop tools, including `screenshot`, `click`, and
`type`. Desktop calls in the same model response run in order. OpenClaw's
default tool discovery and built-in tool policy remain in effect.

Memory search and subagents use OpenClaw's defaults. Memory is scoped to the
episode state. Semantic memory search requires a supported embedding provider;
without one OpenClaw may fall back to keyword search. We do not configure a
browser, messaging channel, or embedding provider. Heartbeat and cron also use
upstream defaults, but jobs cannot run after the episode gateway stops. ALE does
not create a separate persistent scheduler. Background `exec` processes end with
the Gateway; the agent must collect required command results before finishing.

## Config

```yaml
agent:
  harness: openclaw_cli
  model: openai/gpt-5.4
  config:
    provider: openrouter   # or "direct" / "zai" / "custom"
    cli_version: "2026.10.5"
    agent_timeout_s: 1800
    thinking: high
    cua_plugin_path: /opt/ale/openclaw-cua-plugin
    cua_plugin_repo: https://github.com/cua-verse/openclaw.git
    cua_plugin_branch: agenthle
```

| Field | Meaning |
|---|---|
| `provider` | Primary routing: `openrouter` (default), `direct`, `zai`, or `custom` |
| `provider_id` | OpenClaw provider id for a custom route |
| `model_id` | Provider-facing model or deployment id for a custom route |
| `base_url` | Optional OpenAI-compatible endpoint override |
| `api_key_env` | Environment variable containing a custom provider key |
| `provider_api` | OpenClaw transport for a custom provider; defaults to `openai-completions` |
| `supports_usage_in_streaming` | Request streamed token usage metadata; defaults to `true` |
| `model` | Model slug. OpenRouter: `openai/gpt-5.4`, `anthropic/claude-sonnet-4-6`. Direct: `gpt-5.4` or `claude-sonnet-4-6` |
| `model_params` | Provider-specific model parameters; use `extra_body` for raw request fields |
| `cli_version` | Exact official npm package version; defaults to `2026.10.5` |
| `agent_timeout_s` | Overall OpenClaw agent-run timeout in seconds |
| `llm_idle_timeout_s` | Legacy setting. Official OpenClaw 2026.10.5 does not support it; the upstream timeout policy applies |
| `thinking` | Provider-specific reasoning depth; common values are `off`, `on`, `low`, `medium`, `high`, and `max` |
| `vision_model` | Per-tool model override for image analysis |
| `vision_provider` | Optional independent route for `vision_model`; `null` inherits `provider` |
| `vision_base_url` | Optional endpoint override for the vision route |
| `vision_api_key` | Optional key override for the vision route |
| `tools_deny` | Tools removed from the schema |
| `plugins_allow` | Optional plugin allowlist; empty uses OpenClaw defaults |
| `plugins_deny` | Optional plugin denylist; empty uses OpenClaw defaults |
| `heartbeat_every` | Optional cadence; `null` uses the upstream default, `"never"` disables recurring heartbeats |
| `tarball_path` | Optional package tarball inside sandbox |
| `tarball_url` | Optional package URL when `tarball_path` is empty or missing |
| `cua_plugin_path` | Path to CUA plugin source inside sandbox |
| `cua_plugin_repo` | Git URL to clone CUA plugin source (fallback when `cua_plugin_path` is missing) |
| `cua_plugin_branch` | Branch to clone for CUA plugin source (default: `agenthle`) |

## Output

`--json` writes a JSON envelope to stdout or stderr. Key fields:

- `meta.finalAssistantVisibleText` -> final agent text
- `meta.agentMeta.sessionId` -> session id
- `meta.agentMeta.usage.{input,output,total}` -> token counts
- `meta.executionTrace.{winnerProvider,winnerModel}` -> actual provider/model

The main session is exported from SQLite to `transcript.jsonl`; subagent
sessions and their relationship index are exported under `subagents/`. Legacy
JSONL sessions are also recognized. The gateway log is `gateway.log`.

`parse_artifacts()` reads the transcript JSONL and result envelope to
populate the `TrajectoryBuilder` with agent steps, tool calls, tool
results, and usage metrics.

## Layout

```
ale_run/agents/openclaw_cli/
├── __init__.py     — re-exports OpenClawCliConfig, OpenClawCliDeployer
├── config.py       — OpenClawCliConfig (standalone dataclass)
├── deployer.py     — OpenClawCliDeployer (BaseAgentDeployer: install/launch/parse_artifacts)
├── runtime.py      — private CLI and Node installation
├── README.md       — this file
└── AGENTS.md       — integration notes, tool classification, install details
```
