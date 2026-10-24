# Droid (Factory AI CLI) Integration Notes

## Source And Fork

- Upstream: Factory.ai's closed-source `droid` binary (no public source repo,
  no npm package — distributed only via the `app.factory.ai/cli` installer).
- Fork: **none** (closed-source). All ALE-specific behavior lives in
  `deployer.py` and the YAML config.
- Default pinned version: **`0.244.0`**.

The `app.factory.ai/cli` shell installer downloads from
`https://downloads.factory.ai/factory-cli/releases/<VER>/<platform>/<arch>/droid[.exe]`.
Direct-download URLs are stable per version.

## Install

### Auto-install (sandbox deployer)

The `DroidDeployer.install()` method checks the actual version, installs the
exact configured release if necessary, then checks it again. It does not fall
back to an unpinned latest version:
   ```bash
   VER=0.244.0
   curl -fsSL "https://downloads.factory.ai/factory-cli/releases/${VER}/linux/x64/droid" \
     -o ~/.local/bin/droid && chmod +x ~/.local/bin/droid
   ```

The Linux installer places the binary at `~/.local/bin/droid`.

Use `linux/x64-baseline` if `/proc/cpuinfo` does not advertise `avx2`.

### Uninstall

```bash
rm -rf ~/.local/bin/droid ~/.factory
```

## CLI Invocation

Verified headless invocation (droid 0.116.0):

```bash
droid exec \
    -f /path/to/prompt.txt \
    -m anthropic/claude-sonnet-4-6 \
    --output-format stream-json \
    --skip-permissions-unsafe \
    --reasoning-effort medium \
    --cwd /home/kasm-user/work
```

Required flags:

| Flag | Reason |
|---|---|
| `-f <prompt-file>` | UTF-8-safe prompt input |
| `-m <model-id>` | OpenRouter model slug; matches `customModels[0].model` |
| `--output-format stream-json` | NDJSON event stream the deployer parses |
| `--skip-permissions-unsafe` | Allows ALL tools; only fully-headless mode |
| `--cwd <dir>` | Pin the working dir |

Optional:

| Flag | Reason |
|---|---|
| `--reasoning-effort {off\|none\|low\|medium\|high}` | Defaults per model |
| `--disabled-tools <csv>` | Explicit tool exclusions; empty by default |
| `--enabled-tools <csv>` | Whitelist; rarely needed |

## Auth

The current CLI requires a valid `FACTORY_API_KEY` or an authenticated
Factory session, separately from the model provider's API key. Version
0.244.0 rejects the placeholder previously accepted by 0.116.0.

The OpenRouter API key goes into `~/.factory/settings.json`:

```json
{
  "customModels": [{
    "model": "anthropic/claude-sonnet-4-6",
    "displayName": "anthropic/claude-sonnet-4-6 [OpenRouter]",
    "baseUrl": "https://openrouter.ai/api/v1",
    "apiKey": "<OPENROUTER_API_KEY>",
    "provider": "generic-chat-completion-api",
    "maxOutputTokens": 128000
  }]
}
```

## Verified Event Shapes (droid 0.116.0, --output-format stream-json)

```
{"type":"system","subtype":"init","cwd","session_id","tools":[...],"model","reasoning_effort"}
{"type":"message","role":"user","id","text","timestamp","session_id"}
{"type":"message","role":"assistant","id","text","timestamp","session_id"}
{"type":"tool_call","id":"toolu_...","messageId","toolId","toolName","parameters":{...},"timestamp","session_id"}
{"type":"tool_result","id":"toolu_...","messageId","toolId","isError":bool,"value":"<text>","timestamp","session_id"}
{"type":"error","source":"agent_loop"|"cli","message","timestamp","session_id"}
{"type":"completion","finalText","numTurns","durationMs","usage":{...}}
```

Per-session origin log:
- `~/.factory/sessions/<cwd-encoded>/<session-id>.jsonl`
- `~/.factory/logs/droid-log-single.log`
- `~/.factory/logs/console.log`

## Tool Classification

No tools are excluded by default. Interactive and cloud-backed tools may not
work in a headless sandbox without their external services.

Built-in: Read, Edit, Execute, Glob, Grep, LS, Create, ApplyPatch,
FetchUrl, TodoWrite, Skill, Task, ToolSearch, WebSearch, Write.

CUA MCP (14 tools via bridge): `cua___screenshot`, `cua___click`,
`cua___type`, `cua___key`, `cua___key_down`, `cua___key_up`,
`cua___hold_key`, `cua___mouse_move`, `cua___mouse_down`,
`cua___mouse_up`, `cua___drag`, `cua___scroll`, `cua___wait`,
`cua___cursor_position`.

## Known Issues

### Provider routing (byok_provider selection)

| `byok_provider` value | Endpoint | Status |
|---|---|---|
| `generic-chat-completion-api` | `/v1/chat/completions` | **Working** — verified with OpenRouter |
| `openai` | `/v1/chat/completions` then `/v1/responses` | ⚠️ OpenRouter doesn't implement Responses API; may retry forever |
| `anthropic` | `/v1/messages` | ⚠️ Empty-body retry loop on OpenRouter |

Use `generic-chat-completion-api` for OpenRouter.

### ARM64 not supported

The droid binary is x86_64-only. Docker images running on ARM64 hosts
(e.g. Apple Silicon Macs) will fail at install with
`Unsupported platform: linux-arm64`. Use an AMD64 machine or cross-arch
emulation.
