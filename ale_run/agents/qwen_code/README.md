# Qwen Code

Runs the official `@qwen-code/qwen-code` CLI in ALE Linux and native Windows
sandboxes. The pinned release is **0.25.3**. Installation verifies the package
metadata and CLI version, invokes the private installation directly, and uses
Node >=22 (installing a private Node 24.14.0 when the image runtime is older).
The sandbox's desktop profile and GUI bridge runtime are preserved.

## Authentication and models

Use `configs/agents/qwen_code.yaml` with `QWEN_API_KEY` for QwenCloud's
`qwen3.8-max`. The endpoint and model are explicit configuration; the harness
does not infer a provider from the model name.

Qwen Code also supports OpenAI-compatible providers. No Qwen subscription or
Qwen key is required when routing the harness through another provider:

```yaml
harness: qwen_code
model: gpt-4.1
config:
  base_url: https://api.openai.com/v1
  api_key: ${env:OPENAI_API_KEY}
  auth_type: openai
```

The `configs/agents/qwen_code_openai.yaml` preset uses `gpt-5.4-mini` with high
reasoning effort over Responses for GUI evaluation. The pinned upstream Chat
Completions adapter sends `max_tokens`, which GPT-5 endpoints reject; use
`openai-responses` for those models. The existing
`qwen_code_hf.yaml` preset uses a separate `HF_INFERENCE_TOKEN`. A provider model
must support tool calling and image input for GUI tasks. `auth_type: openai`
uses Chat Completions; `openai-responses` selects Qwen's native Responses adapter.
The endpoint must implement the selected protocol. API keys stay in the process
environment, and known credentials are redacted before logs are mirrored.

## Configuration

| Field | Default | Meaning |
| --- | --- | --- |
| `cli_version` | `0.25.3` | Exact official npm version; no moving tags |
| `model` | `qwen3.8-max` | Provider model ID |
| `base_url` | `https://maas.qwencloudapi.com/compatible-mode/v1` | API root |
| `api_key` | null | Explicit credential, normally `${env:...}` |
| `api_key_env` | `OPENAI_API_KEY` | Environment fallback when `api_key` is unset |
| `auth_type` | `openai` | `openai` or `openai-responses` |
| `reasoning_effort` | null | Provider default, or `none`, `low`, `medium`, `high`, `xhigh`, `max`; upstream maps/clamps to model support |
| `context_window` | null | Qwen `model.generationConfig.contextWindowSize` |
| `auto_compact_token_limit` | null | Requires `context_window`; maps to `context.autoCompactThreshold` as a fraction between 0.01 and 1 |
| `max_output_tokens` | null | Qwen generation `samplingParams.max_tokens`; provider adapter maps this to its output limit |
| `max_turns` | null | `--max-session-turns` |
| `max_tool_calls` | null | `--max-tool-calls` |
| `max_subagent_depth` | null | `--max-subagent-depth` |
| `coordinate_space` | `pixel` | GUI coordinates: `pixel` or `normalized` |
| `telemetry_enabled` | true | Local native OpenTelemetry capture |
| `disabled_tools` | empty | Additional tool exclusions |
| `extra_envs` | empty | Extra process environment; harness-owned routing, state, and telemetry settings take precedence |

Unset limits retain upstream defaults. Auto-compaction is a ceiling: upstream
may compact earlier to reserve summarization space. The episode wall timeout is
owned by ALE orchestration. These settings do not change that timeout.

## Execution and GUI tools

Each launch gets fresh `QWEN_HOME` and `QWEN_RUNTIME_DIR` directories outside the
collected work directory. `HOME`, `USERPROFILE`, and desktop application profiles
are preserved. Windows uses a short working directory to avoid long native chat
paths. The harness records its effective settings in `qwen-settings.json`.
Prompts are read through stdin from a file, avoiding Windows command-line limits.

Headless execution uses `--approval-mode yolo` and does not exclude tools or
restrict MCP discovery by default. Interactive tools may not work in headless
mode. The bundled
`computer-use` and `browser-use` skills target Qwen's separate Node REPL/SDK
setup and are disabled in ALE. An appended system instruction directs GUI work
to the already configured `cua` tools and screenshot observations. The pinned CLI skips
its startup update check. The ALE VM supplies the execution isolation boundary.

The required CUA server uses the sandbox's Node executable and refreshed bridge
source. Its environment includes `CUA_SERVER_URL`, `CUA_SERVER_PLATFORM`, and
`CUA_COORDINATE_SPACE`. Missing bridge files or a disconnected `cua` initialization
event fail the run. This matches the GUI bridge used by Codex and Claude Code.
Qwen bounds large MCP images to its visual budget (for example, 1920x1080 becomes
1456x819). The appended instruction explains how to scale pixel positions using
`get_screen_size`. Normalized coordinates are also supported. The upstream CLI
and shared GUI bridge remain unmodified.

Completion requires a successful native terminal result and connected GUI MCP.
A zero exit code alone is insufficient. The harness allows ten seconds for exit
after the final result, then reaps lingering processes. It owns a process group
and subreaper on Linux and a nested Job Object on Windows, including during cancellation.
Windows CLI processes run without a console so closing a desktop console window
cannot send a close event to the agent itself.

## Trajectories and telemetry

The incrementally mirrored artifacts are:

- `stdout.log`: native stream JSON, including partial messages.
- `stderr.log`: diagnostic output.
- `event_times.jsonl`: stdout receipt times, explicitly distinguished from native timestamps.
- `native_chats.jsonl`: native chat records, including full tool results and images that stdout replaces with placeholders.
- `telemetry.jsonl`: native OpenTelemetry records converted from concatenated JSON objects to JSONL.

Postprocessing writes `transcript.jsonl` and `telemetry_summary.json`, restores
screenshots into typed image observations, unwraps `tool_call` into the underlying
MCP tool, and separates child trajectories. Truncated output preserves partial
reasoning and tool arguments. Terminal cumulative usage overrides step sums;
missing cost is not estimated.

Background memory agents can emit only telemetry, without full conversational
output. Their recorded calls, tool execution status, and per-response usage are retained in
separate child trajectories with `result_content_available: false`. No tool
output is invented. Children without a terminal event have no final metrics and
explicitly report `completion_status: unreported`.
`trajectory.extra.qwen_code.tool_completeness` reports calls, matched results,
results with content, status-only results, missing/orphan results, and native
telemetry tool-event counts.
Product usage statistics and remote telemetry export are disabled. Local native
telemetry still contains tool arguments and model output and is treated as an
experiment artifact.

## Validation

Run the regression suite:

```sh
uv run pytest tests/ale_run/test_qwen_code.py tests/ale_run/test_main_harness_regressions.py
```

To include the unmodified CLI contract test, install the pinned package outside
the repository and set `QWEN_TEST_NODE` to a Node >=22 executable and
`QWEN_TEST_CLI` to its `@qwen-code/qwen-code/cli-entry.js`. The test uses a local
OpenAI-compatible server and stdio MCP fixture, requires no API key, and verifies
real tool discovery, image delivery, native telemetry, and trajectory recovery.
It runs on Linux and native Windows.

For real GUI validation, select `configs/agents/qwen_code_openai.yaml` in an
experiment with `selected_tasks/hello_both.txt`, an environment supporting both
OS images, `output_path: null`, and the `prompt_suffix` from `example_exp.yaml`.
Inspect the run status, evaluator score, screenshots, and tool-completeness
metadata. A successful evaluator score does not by itself establish normal CLI
termination. Run evidence and platform results belong in the PR description.

## Upstream references

- [Overview](https://qwenlm.github.io/qwen-code-docs/en/users/overview/)
- [Headless mode](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/)
- [Authentication](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/)
- [Model providers](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/model-providers/)
- [Settings](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/settings/)
- [MCP](https://qwenlm.github.io/qwen-code-docs/en/developers/tools/mcp-server/)
- [Telemetry](https://qwenlm.github.io/qwen-code-docs/en/developers/development/telemetry/)
- [Pinned source](https://github.com/QwenLM/qwen-code/tree/v0.25.3)
