# Muse Code

ALE runs the official Muse Code CLI natively inside Linux and Windows sandboxes.
Select `configs/agents/muse_code.yaml` in the experiment's `agents` list and put
`META_API_KEY` in `secret/.env`. The preset selects
[Muse Wonder](https://dev.meta.ai/docs/models), using the official API model ID
`muse-wonder-1.0`. Model access is account-specific; local experiment configs can
override `model` with an ID returned by your account's `/v1/models`.
A `base_url` override must provide Meta-compatible Responses and model endpoints.

The CLI is pinned to `1.4.10-R6427.1`. Installation selects the official Linux or
Windows artifact for x86-64 or ARM64, verifies the manifest's version, size and
SHA-256, then checks the executable's full version. Downloads are staged on the
installation filesystem. A matching installed binary is reused. ALE runs the
native executable directly, so the self-updating launcher cannot change the
release during an experiment. Windows uses the native executable and PowerShell,
without WSL or a shell wrapper.

## Configuration and parity

The [annotated preset](../../../configs/agents/muse_code.yaml) lists every exposed
setting. `config.py` validates ranges before allocating the sandbox. Additional
native settings can be supplied as dotted JSON keys in `settings_overrides`;
Muse itself validates their schema. MCP and telemetry settings are harness-owned.
Do not put credentials in overrides; use `META_API_KEY` or `config.api_key`.

| Capability | Muse configuration and behavior |
| --- | --- |
| Model, gateway, credentials | `model`, `base_url`, `api_key`; credentials travel through ALE's secret sidecar/environment, never CLI arguments. |
| Reproducible CLI | `cli_version` pins the complete official release identifier. |
| Unattended execution | `yolo: true`, matching Codex/Claude sandbox execution; `request_user_input` is always disabled. |
| Explicit permission policy | With `yolo: false`, `approval_mode`, `sandbox_network`, and `trust_workspace` configure native policy. |
| Reasoning | `reasoning_effort: high`; accepts the native minimal/low/medium/high/xhigh/max/ultra values. The selected model must support the value. |
| Model-step budget | `max_turns` maps to `--max-model-steps`; null or -1 leaves it uncapped. Experiment `wall_time_s` owns the elapsed-time budget. |
| Context | `context_window` sets the native provider context limit. `auto_compact_token_limit` requires it and converts an absolute limit into a soft fraction. |
| Compaction | Optional native strategy and soft/hard fractions; unset values keep native behavior. |
| Tool execution | `parallel_tool_calls`, `max_tool_output_bytes`, `execution_capacity`; unset values keep native defaults. |
| Tool selection | Optional `toolset` allowlist, always extended with `mcp__cua__*`; native web tools and reminders have independent disable switches. |
| Personal context | `no_foreign_personal_context: true` excludes personal rules/skills imported from other harnesses. |
| GUI MCP | The same ALE `cua_mcp_server`, sandbox Node path, platform, and server URL used by Codex/Claude. `coordinate_space: pixel` is explicit; normalized coordinates are optional. |
| Telemetry | `telemetry_enabled: true` derives API usage/timings, provider attempts and tool timings from native session events. |
| Cancellation | Detached process group on Linux; tree termination on Windows. ALE's sandbox supervisor also contains descendants, including native Windows Job Objects. |

The GUI server is required: startup fails if it cannot initialize. Muse settings
include `schema_version: 1` and `mcpServers.cua` with stdio transport. Muse settings are written in the sandbox user's native Muse configuration
directory and copied to `muse-settings.json` for audit. The harness preserves the
sandbox's XDG environment so desktop applications keep their configured profiles.
Only sessions created after launch are mirrored and copied into a fresh evidence
directory, including retries in an existing work directory.

Although CLI help advertises `none`, this release rejects it for the Meta provider;
ALE rejects it during configuration validation.

Muse does not expose Claude's dollar budget or output/thinking-token budget as
verified equivalents in this release. These are not silently emulated. Use its
native reasoning/model-step controls and the experiment wall-clock budget.
Likewise, Muse's native telemetry is not a Codex/Claude OTLP stream.

## Artifacts

- `transcript.jsonl` and `stderr.log`: incrementally collected CLI output.
- `native_events.jsonl`: incrementally mirrored, credential-redacted native session records, including child sessions.
- `muse-settings.json`: the exact native MCP configuration.
- `native_data_path.json` and `muse-data/<launch>/`: select and retain the native logs for this launch, excluding prior sessions and application profiles.
- `telemetry.jsonl`: normalized native events, including provider diagnostics and compaction events.
- `telemetry_summary.json`: API completion usage, provider attempts/response IDs when reported, tool lifecycle timings/errors, terminal state, and event counts.
- The standard ALE `trajectory.json`: assistant messages, reasoning text/summaries when exposed (encrypted reasoning remains in native logs), tool calls/results, screenshots, token/cache accounting and child trajectories.

Parsing merges the incremental mirror with final native session logs and
deduplicates record IDs. Partial/malformed records are tolerated; unanswered tool
calls remain marked incomplete. A zero CLI exit code only counts as completion
when stdout also contains a successful terminal event.

Timestamps come from the durable native session records. The CLI's stdout event
clock can be synthetic and is not used for trajectory timestamps. API durations
are native `model_completed.duration_ms`; tool durations use matching native
start/end records. Missing duration evidence stays unknown. Muse does not report
an authoritative dollar cost here: telemetry stores `cost_usd: null`. ALE's
schema-default `total_cost_usd: 0` must not be interpreted as free usage.

## Validation

Native tests and GUI demos cover Linux and Windows x86-64. ARM64 artifact
selection is unit-tested only.

Run unit tests with the repository's locked development environment:

```bash
uv run pytest tests/ale_run/test_muse_code.py -q
```

The optional native contract tests exercise the unmodified pinned CLI against a
local deterministic Responses endpoint and stdio MCP server. They require no
credentials and cover configuration, screenshots, tool errors, reasoning,
Unicode paths, token accounting, and provider diagnostics:

```bash
ALE_MUSE_TEST_BINARY=/absolute/path/to/muse \
  uv run pytest tests/ale_run/test_muse_code_native.py -q
```

On Windows set `$env:ALE_MUSE_TEST_BINARY` to the pinned `muse.exe` before running
the same pytest command. Contract tests do not substitute for a real-model demo.

For full validation, copy `example_exp.yaml`, select this agent and an environment
supporting both operating systems, and retain `selected_tasks/hello_both.txt` plus
the example's exact `prompt_suffix`. Set environment `output_path: null` and
experiment `cleanup_mode: keep` to inspect deliverables in the sandboxes. Run:

```bash
uv run python -m ale_run run muse_demo.yaml --dry-run
uv run python -m ale_run run muse_demo.yaml
```

Verify both `demo/hello` and `demo/hello_win` score 1, inspect the actual GUI tool
calls/screenshots and output file, and check saved telemetry before releasing
the retained sandboxes. Reuse matching completed results when validating again.

## Official references

References for the pinned release:

- [Muse Code overview](https://dev.meta.ai/docs/muse-code)
- [Configuration](https://dev.meta.ai/docs/muse-code/configuration)
- [Extensions and MCP](https://dev.meta.ai/docs/muse-code/extending)
- [Permissions](https://dev.meta.ai/docs/muse-code/permissions)
- [Authentication](https://dev.meta.ai/docs/muse-code/auth)
- [Changelog](https://dev.meta.ai/docs/muse-code/changelog)
- [Models and access](https://dev.meta.ai/docs/models)
- [Responses protocol](https://dev.meta.ai/docs/protocols/responses)
