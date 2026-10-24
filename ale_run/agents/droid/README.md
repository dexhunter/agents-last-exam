# Droid (Factory AI CLI) Agent

Droid is Factory.ai's closed-source binary CLI. It runs inside the
sandbox as a one-shot `droid exec` process and reaches the desktop
through the shared CUA MCP Server bridge:

```text
droid exec -f <prompt> -m <model> --output-format stream-json
           --skip-permissions-unsafe --cwd <workdir>
  -> CUA MCP Server (stdio, ~/.factory/mcp.json)
  -> CUA Computer Server :8000
  -> desktop / filesystem
```

`droid` keeps its native built-in tools (Read, Edit, Execute, Glob,
Grep, LS, Create, ApplyPatch, FetchUrl, TodoWrite, Skill, Task,
ToolSearch) alongside the CUA bridge tools. The deployer ships with
a `disabled_tools` baseline that turns off Factory-cloud-only / SaaS
integrations and interactive helpers that block headless runs.

## Architecture

Distribution: pre-built binary downloaded by the official Factory.ai
installer. No npm package, no source repo, no fork. The deployer
installs the configured version if the existing binary is missing or mismatched.

| | Path |
|---|---|
| Binary | `~/.local/bin/droid` |
| Config home | `~/.factory/` |
| settings.json | `~/.factory/settings.json` |
| mcp.json | `~/.factory/mcp.json` |
| Sessions | `~/.factory/sessions/<cwd-encoded>/<sid>.jsonl` |
| Logs | `~/.factory/logs/{droid-log-single.log,console.log}` |

Pinned version: `0.244.0`.

## Provider — OpenRouter BYOK

The deployer writes a single `customModels` entry into
`~/.factory/settings.json` pointing at `https://openrouter.ai/api/v1`.
The Factory.ai direct path is not shipped.

The CLI also needs a valid `FACTORY_API_KEY` or an authenticated Factory
session. This is separate from the model provider's API key.

## Install

The deployer downloads the exact configured release from
`https://downloads.factory.ai/factory-cli/releases/<version>/linux/x64/droid`
and verifies its version after installation.

## Config

```yaml
agent:
  harness: droid
  model: anthropic/claude-sonnet-4-6
  config:
    cli_version: "0.244.0"
    reasoning_effort: medium
    skip_permissions_unsafe: true
    max_output_tokens: 128000
    byok_provider: generic-chat-completion-api
```

| Field | Meaning |
|---|---|
| `model` | OpenRouter model slug; written into `customModels[0].model` and passed to `-m` |
| `reasoning_effort` | `off\|none\|low\|medium\|high` — passed via `--reasoning-effort` |
| `skip_permissions_unsafe` | `true` (default) → `--skip-permissions-unsafe`; `false` leaves the CLI permission default |
| `max_output_tokens` | Written into `customModels[0].maxOutputTokens` |
| `byok_provider` | Written into `customModels[0].provider`. Use `generic-chat-completion-api` for OpenRouter |
| `disabled_tools` | List of droid tool names passed to `--disabled-tools` |

## Bridge

`install()` writes `~/.factory/mcp.json` with a single stdio entry
pointing at the CUA MCP server (same bridge as claude_code, cursor_cli):

```json
{
  "mcpServers": {
    "cua": {
      "type": "stdio",
      "command": "<node>",
      "args": ["<mcp_server_dir>/src/index.js"],
      "disabled": false
    }
  }
}
```

## Output Format

`--output-format stream-json` produces NDJSON with these event types
(verified on droid 0.116.0):

- `system / init` — `cwd`, `session_id`, `tools[]`, `model`, `reasoning_effort`
- `message / role:user|assistant` — flat `text` or nested Anthropic-style blocks
- `tool_call` — top-level event with `toolName`, `parameters`
- `tool_result` — paired by `id`, carries `value` (string) + `isError`
- `error / source:cli|agent_loop` — fatal abort
- `completion` — `finalText`, `numTurns`, `durationMs`, `usage`

## Execution

`launch()` writes the prompt to a file and spawns `droid exec` as a
child process with stdout/stderr captured. The experiment's wall-time budget
controls the deadline. The Factory credential is passed through unchanged.

## Layout

```
ale_run/agents/droid/
├── __init__.py     — re-exports DroidConfig, DroidDeployer
├── config.py       — DroidConfig (standalone dataclass)
├── deployer.py     — DroidDeployer (install/launch/parse_artifacts)
├── README.md       — this file
└── AGENTS.md       — integration notes, tool classification, known issues
```
