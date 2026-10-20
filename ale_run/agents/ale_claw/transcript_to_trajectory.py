"""Parse OpenClaw on-disk transcripts into ALE-v1.0 Trajectory steps.

OpenClaw (the harness in :mod:`ale.agents.ale_claw.harness`) writes per-run
artifacts under ``<work_dir>/openclaw_sessions/<task_id>/``:

  ├── transcript.jsonl      — append-only JSONL: session header, message entries,
  │                            compaction entries
  └── state.json            — running totals (token usage, step count,
                              compaction count, model)

And per-turn API payloads under
``<work_dir>/trajectories/<traj_id>/turn_NNN/<NNNN>_api_result.json``
(LiteLLM-shape OpenAI response — preserves cache token breakdown).

This module parses those into ALE Steps via :class:`TrajectoryBuilder`.
**No InteractionLog / InteractionStep intermediate** — we emit ATIF Steps
directly. Logic largely ported from agenthle's
``orchestration/agents/ale_claw/transcript_log.py``; key differences:

  - No agenthle deps — only stdlib + ``ale.agents.trajectory``.
  - Drop the ``InteractionLog.save(...)`` step. Caller's :meth:`collect`
    drives the trajectory builder directly.
  - Per-turn assistant messages collapse into ONE :class:`Step`
    (text → ``message``, thinking → ``reasoning``, function_calls →
    ``tool_calls``).
  - Per-message usage → :class:`StepMetrics` on that step.
  - Aggregated totals (state.json + per-turn cache) land in
    ``builder.trajectory.extra["ale_claw"]["usage"]`` for downstream
    consumers that want exact accounting (the default
    ``builder.finalize`` sum lacks cache_read/cache_write since the
    transcript itself doesn't carry them).
"""
from __future__ import annotations

import ast
import base64
import json
import logging
from pathlib import Path
from typing import Any

from ale_run.base_interface import (
    ContentPart,
    ImageSource,
    Observation,
    StepMetrics,
    ToolCall,
    ToolResult,
    TrajectoryBuilder,
)

logger = logging.getLogger(__name__)

# Subdirs the harness writes under our work_dir. Mirror the agenthle layout
# so transcript shape is unchanged.
_SESSIONS_SUBDIR = "openclaw_sessions"
_TRAJECTORIES_SUBDIR = "trajectories"


# =============================================================================
# Public entry point
# =============================================================================

def parse_transcripts_into(work_dir: Path, builder: TrajectoryBuilder) -> None:
    """Walk OpenClaw artifacts under ``work_dir`` → emit Steps into ``builder``.

    Idempotent on partial / missing data: emits a single ``system`` step
    when no transcript is found, otherwise appends one Step per assistant
    turn + one per tool reply, then writes aggregate usage to
    ``builder.trajectory.extra["ale_claw"]["usage"]``.
    """
    sessions_root = work_dir / _SESSIONS_SUBDIR
    transcripts = sorted(sessions_root.glob("*/transcript.jsonl")) if sessions_root.is_dir() else []
    if not transcripts:
        builder.add_step(
            source="system",
            message="ale-claw: no transcript at "
                    f"{sessions_root}/*/transcript.jsonl",
            extra={"reason": "no_transcript", "expected_root": str(sessions_root)},
        )
    for path in transcripts:
        _parse_one_transcript(path, builder, work_dir)

    child_input = child_output = 0
    child_cache_read = child_cache_write = 0
    child_cost = 0.0
    child_cache_incomplete = child_cost_incomplete = False
    session_dirs = sorted(path for path in sessions_root.glob("*") if path.is_dir()) if sessions_root.is_dir() else []
    for session_dir in session_dirs:
        registry_path = session_dir / "subagent-runs.jsonl"
        records: dict[str, dict[str, Any]] = {}
        if registry_path.is_file():
            for line in registry_path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("run_id"):
                    records[record["run_id"]] = record
        child_root = session_dir / "subagents"
        child_ids = {child_path.parent.name for child_path in child_root.glob("*/transcript.jsonl")}
        child_ids.update(run_id for run_id in records if Path(run_id).name == run_id and run_id not in (".", ".."))
        for run_id in sorted(child_ids):
            child_path = child_root / run_id / "transcript.jsonl"
            record = records.get(run_id, {})
            child = TrajectoryBuilder(
                agent_name="ale_claw", model=record.get("model"),
                task_path=builder.trajectory.task_path,
                variant_index=builder.trajectory.variant_index,
                instruction=record.get("task", ""),
            )
            if child_path.is_file():
                _parse_one_transcript(child_path, child, work_dir)
            else:
                child.add_step(source="system", message="ale-claw: subagent transcript unavailable")
            usage = record.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            child_input += input_tokens
            child_output += output_tokens
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_write = int(usage.get("cache_write_input_tokens") or 0)
            child_cache_read += cache_read
            child_cache_write += cache_write
            cost = usage.get("cost_usd")
            if isinstance(cost, (int, float)):
                child_cost += cost
            if input_tokens or output_tokens:
                child_cache_incomplete |= "missing_cache_calls" not in usage or bool(usage["missing_cache_calls"])
                child_cost_incomplete |= (
                    not isinstance(cost, (int, float))
                    or "missing_cost_calls" not in usage
                    or bool(usage["missing_cost_calls"])
                )
            child.override_final_metrics(
                total_input_tokens=input_tokens, total_output_tokens=output_tokens,
                total_cache_read_tokens=cache_read, total_cache_creation_tokens=cache_write,
                total_cost_usd=cost if isinstance(cost, (int, float)) else None,
            )
            if (input_tokens or output_tokens) and (
                not isinstance(cost, (int, float))
                or "missing_cost_calls" not in usage
                or usage["missing_cost_calls"]
            ):
                child.mark_cost_unavailable()
            child.trajectory.extra["ale_claw"] = {"run_id": run_id, "status": record.get("status")}
            status = "completed" if record.get("status") == "complete" else "failed"
            builder.trajectory.subagent_trajectories.append(child.finalize(reward=None, status=status))

    aggregated = _aggregate_usage(work_dir)
    aggregated["overall_input_tokens"] += child_input
    aggregated["output_tokens"] += child_output
    aggregated["cache_read_input_tokens"] = aggregated.get("cache_read_input_tokens", 0) + child_cache_read
    aggregated["cache_write_input_tokens"] = aggregated.get("cache_write_input_tokens", 0) + child_cache_write
    if not child_cache_incomplete:
        aggregated["uncached_input_tokens"] += max(child_input - child_cache_read - child_cache_write, 0)
    elif child_input:
        aggregated["unclassified_child_input_tokens"] = max(child_input - child_cache_read - child_cache_write, 0)
    if child_cost:
        aggregated["total_cost_usd"] = aggregated.get("total_cost_usd", 0) + child_cost
    helper_calls = _read_helper_usage(work_dir / "helper_usage.jsonl")
    if helper_calls:
        helper_input = sum(int(call.get("input_tokens") or 0) for call in helper_calls)
        helper_output = sum(int(call.get("output_tokens") or 0) for call in helper_calls)
        helper_cached = sum(int(call.get("cache_read_input_tokens") or 0) for call in helper_calls)
        helper_write = sum(int(call.get("cache_write_input_tokens") or 0) for call in helper_calls)
        aggregated["overall_input_tokens"] += helper_input
        aggregated["output_tokens"] += helper_output
        aggregated["cache_read_input_tokens"] = aggregated.get("cache_read_input_tokens", 0) + helper_cached
        aggregated["cache_write_input_tokens"] = aggregated.get("cache_write_input_tokens", 0) + helper_write
        helper_unclassified = sum(
            max(int(call.get("input_tokens") or 0) - int(call.get("cache_read_input_tokens") or 0)
                - int(call.get("cache_write_input_tokens") or 0), 0)
            for call in helper_calls
            if call.get("cache_read_input_tokens") is None and call.get("cache_write_input_tokens") is None
        )
        aggregated["uncached_input_tokens"] += max(helper_input - helper_cached - helper_write - helper_unclassified, 0)
        if helper_unclassified:
            aggregated["unclassified_helper_input_tokens"] = helper_unclassified
        if any(call.get("cache_read_input_tokens") is None and call.get("cache_write_input_tokens") is None for call in helper_calls):
            aggregated["helper_cache_incomplete"] = True
        known_cost = sum(float(call["cost_usd"]) for call in helper_calls if isinstance(call.get("cost_usd"), (int, float)))
        if known_cost:
            aggregated["total_cost_usd"] = aggregated.get("total_cost_usd", 0) + known_cost
        aggregated["helper_calls"] = len(helper_calls)
        if any(call.get("input_tokens") is None or call.get("output_tokens") is None for call in helper_calls):
            aggregated["helper_usage_incomplete"] = True
        if any(call.get("cost_usd") is None for call in helper_calls):
            aggregated["helper_cost_incomplete"] = True
    if child_cache_incomplete:
        aggregated["child_cache_incomplete"] = True
    if child_cost_incomplete:
        aggregated["child_cost_incomplete"] = True
    builder.trajectory.extra.setdefault("ale_claw", {})["usage"] = aggregated
    if transcripts:
        builder.trajectory.extra["ale_claw"]["raw_transcript"] = str(transcripts[0])
    _reconcile_final_metrics(builder, aggregated)


def _read_helper_usage(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    calls = []
    for line in lines:
        try:
            call = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(call, dict):
            calls.append(call)
    return calls


def _reconcile_final_metrics(
    builder: TrajectoryBuilder, aggregated: dict[str, Any]
) -> None:
    """Feed the authoritative aggregate into ``final_metrics`` via the builder.

    Reconcile main, child, and helper usage. Incomplete cache, cost, or token
    fields are flagged in the trajectory's aggregate. No-op when no input
    token total is available, preserving the per-step sum for degraded runs.
    """
    if any(aggregated.get(key) for key in (
        "main_cost_incomplete", "child_cost_incomplete", "helper_cost_incomplete"
    )):
        builder.mark_cost_unavailable()
    if aggregated.get("overall_input_tokens", 0) <= 0:
        return
    builder.override_final_metrics(
        total_input_tokens=aggregated.get("overall_input_tokens"),
        total_output_tokens=aggregated.get("output_tokens"),
        total_cache_read_tokens=aggregated.get("cache_read_input_tokens", 0),
        total_cache_creation_tokens=aggregated.get("cache_write_input_tokens", 0),
        # Only override cost when the aggregate has it; else keep per-step sum.
        total_cost_usd=aggregated.get("total_cost_usd"),
    )


# =============================================================================
# Transcript walker
# =============================================================================

def _parse_one_transcript(path: Path, builder: TrajectoryBuilder, work_dir: Path) -> None:
    """Walk one ``transcript.jsonl`` file → append Steps to builder."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:                      # noqa: BLE001
        logger.warning("ale-claw: failed to read %s: %s", path, exc)
        return

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") == "turn":
            calls = []
            for block in entry.get("tool_calls") or []:
                try:
                    args = json.loads(block.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": block.get("arguments")}
                calls.append(ToolCall(id=block.get("id") or "", name=block.get("name") or "computer", arguments=args if isinstance(args, dict) else {"value": args}))
            if calls:
                step = builder.add_step(source="agent", tool_calls=calls)
                if entry.get("timestamp"):
                    step.timestamp = entry["timestamp"]
            continue
        if entry.get("type") != "message":
            continue
        _consume_message(entry, builder, work_dir)


def _consume_message(entry: dict[str, Any], builder: TrajectoryBuilder, work_dir: Path) -> None:
    """One transcript ``{"type":"message", ...}`` entry → one Step.

    Assistant messages become one ``agent`` Step that may carry text
    (``message``), thinking (``reasoning``), and tool_calls (joined
    function_call blocks). Tool messages become one ``environment`` Step
    with all tool_results under ``observation.results``.
    """
    msg = entry.get("message") or {}
    role = msg.get("role")
    content = msg.get("content")
    if not isinstance(content, list):
        return
    usage = msg.get("usage") or {}
    stop_reason = msg.get("stopReason")

    if role == "assistant":
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text") or ""
                if t:
                    text_parts.append(t)
            elif btype == "thinking":
                # Upstream uses {"type":"thinking","thinking":"..."} — falls
                # back to "content" when emitted via newer SDK code paths.
                t = block.get("thinking") or block.get("content") or ""
                if t:
                    reasoning_parts.append(t)
            elif btype == "function_call":
                raw_args = block.get("arguments") or ""
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": raw_args}
                if not isinstance(args, dict):
                    args = {"value": args}
                tool_calls.append(ToolCall(
                    id=block.get("id") or "",
                    name=block.get("name") or "",
                    arguments=args,
                ))
            elif btype == "computer_call":
                args = {k: block[k] for k in ("action", "actions") if k in block}
                tool_calls.append(ToolCall(id=block.get("id") or "", name="computer", arguments=args))

        # Skip empty messages (no text / no thinking / no tool_call) to keep
        # the trajectory tight.
        if not (text_parts or reasoning_parts or tool_calls):
            return

        step = builder.add_step(
            source="agent",
            message="\n".join(text_parts) if text_parts else None,
            reasoning="\n".join(reasoning_parts) if reasoning_parts else None,
            tool_calls=tool_calls,
            metrics=_metrics_from_message_usage(usage),
            extra={"stop_reason": stop_reason} if stop_reason else None,
        )
        if entry.get("timestamp"):
            step.timestamp = entry["timestamp"]
        return

    if role == "tool":
        results: list[ToolResult] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            raw = block.get("content")
            text = _normalize_tool_result_content(raw)
            parts = [ContentPart(type="text", text=text)]
            for image in block.get("images") or []:
                if not isinstance(image, dict):
                    continue
                data = image.get("data")
                image_path = image.get("path")
                if not data and isinstance(image_path, str):
                    path = (work_dir / image_path).resolve()
                    if path.is_relative_to(work_dir.resolve()) and path.is_file():
                        data = base64.b64encode(path.read_bytes()).decode("ascii")
                if isinstance(data, str):
                    parts.append(ContentPart(type="image", image=ImageSource(
                        type="base64", data=data, media_type=image.get("media_type") or "image/png")))
            results.append(ToolResult(
                tool_call_id=block.get("tool_use_id") or "",
                content=parts,
                is_error=bool(block.get("is_error", False)),
            ))
        if results:
            step = builder.add_step(
                source="environment",
                observation=Observation(results=results),
            )
            if entry.get("timestamp"):
                step.timestamp = entry["timestamp"]
        return

    # Other roles (user re-injects from compaction etc.) — log to extra and skip.
    builder.trajectory.extra.setdefault("ale_claw", {}).setdefault(
        "skipped_messages", []
    ).append({"role": role, "content_blocks": len(content)})


def _metrics_from_message_usage(usage: dict[str, Any]) -> StepMetrics | None:
    """OpenClaw transcript usage → :class:`StepMetrics`.

    OpenClaw's per-message usage is ``{"input": N, "output": N, "total": N,
    "cost": F}`` (note the unsuffixed key names). cache_read/cache_write are
    NOT in the transcript — they live in the per-turn ``api_result.json``
    and are surfaced via :func:`_aggregate_usage` into trajectory.extra.
    """
    if not usage:
        return None
    in_t = usage.get("input")
    out_t = usage.get("output")
    cost = usage.get("cost")
    if in_t is None and out_t is None and cost is None:
        return None
    return StepMetrics(
        input_tokens=int(in_t) if in_t is not None else None,
        output_tokens=int(out_t) if out_t is not None else None,
        cost_usd=float(cost) if cost is not None else None,
    )


# =============================================================================
# Tool-result content normalization
# =============================================================================

def _normalize_tool_result_content(raw: Any) -> str:
    """Best-effort convert OpenClaw's tool_result content to JSON text.

    OpenClaw stores tool_result content as ``str(dict)`` (Python repr) rather
    than ``json.dumps(dict)``, e.g. ``"{'success': True, ...}"`` instead of
    ``{"success": true, ...}``. Re-serialize as JSON for downstream
    consumers (matches the InteractionLog the agenthle wrapper used to
    write).

    Pass-through (no rewrite) when:
    - input is not a str (already structured — caller json.dumps it)
    - input doesn't look like a Python repr (treat as plain text)
    - literal_eval fails (treat as plain text — covers shell output,
      stack traces, etc.)
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        return json.dumps(raw, ensure_ascii=False)
    s = raw.strip()
    if not (s.startswith("{") or s.startswith("[")):
        return raw
    try:
        parsed = ast.literal_eval(s)
    except (ValueError, SyntaxError, MemoryError):
        return raw
    if not isinstance(parsed, (dict, list)):
        return raw
    return json.dumps(parsed, ensure_ascii=False, indent=2)


# =============================================================================
# Aggregate usage (state.json + per-turn api_result.json)
# =============================================================================

def _aggregate_usage(work_dir: Path) -> dict[str, Any]:
    """Sum total tokens (state.json) + cache breakdown (per-turn api_result.json).

    Main-session token totals come from state.json and per-turn API artifacts;
    child and helper calls are reconciled separately by the caller.
    """
    state_in, state_out = _aggregate_state_json_tokens(work_dir)
    api = _aggregate_api_result_usage(work_dir)
    cache_read, cache_write = api["cache_read"], api["cache_write"]
    msg_in, msg_out, msg_cost = _aggregate_message_usage(work_dir)

    in_t = max(state_in, msg_in, api["input"])
    out_t = state_out or msg_out
    uncovered = max(in_t - api["input"], 0)
    unclassified = api["unclassified"] + uncovered
    uncached_in = max(in_t - cache_read - cache_write - unclassified, 0)

    out: dict[str, Any] = {
        "uncached_input_tokens": uncached_in,
        "output_tokens": out_t,
        "overall_input_tokens": in_t,
    }
    if api["cost_calls"]:
        out["total_cost_usd"] = round(api["cost"], 6)
    elif msg_cost > 0:
        out["total_cost_usd"] = round(msg_cost, 6)
    if api["missing_cost"] or uncovered or (not api["calls"] and (in_t or out_t)):
        out["main_cost_incomplete"] = True
    if api["missing_cache"] or uncovered or (not api["calls"] and (in_t or out_t)):
        out["main_cache_incomplete"] = True
    if unclassified:
        out["unclassified_main_input_tokens"] = unclassified
    if api["missing_usage"]:
        out["main_usage_incomplete"] = True
    if cache_read > 0:
        out["cache_read_input_tokens"] = cache_read
    if cache_write > 0:
        out["cache_write_input_tokens"] = cache_write
    return out


def _aggregate_state_json_tokens(work_dir: Path) -> tuple[int, int]:
    """Read main-agent input/output token totals from each session's ``state.json``."""
    sessions_root = work_dir / _SESSIONS_SUBDIR
    if not sessions_root.is_dir():
        return 0, 0
    in_t = out_t = 0
    for path in sorted(sessions_root.glob("*/state.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tt = data.get("total_tokens") or {}
        in_t += int(tt.get("input_tokens", 0) or 0)
        out_t += int(tt.get("output_tokens", 0) or 0)
    return in_t, out_t


def _aggregate_api_result_usage(work_dir: Path) -> dict[str, Any]:
    """Sum reported main-model usage and track missing per-call fields."""
    trajectories_root = work_dir / _TRAJECTORIES_SUBDIR
    total: dict[str, Any] = {
        "input": 0, "cache_read": 0, "cache_write": 0, "unclassified": 0,
        "cost": 0.0, "calls": 0, "cost_calls": 0,
        "missing_cache": False, "missing_cost": False, "missing_usage": False,
    }
    for path in trajectories_root.glob("*/turn_*/[0-9]*_api_result.json"):
        total["calls"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            total["missing_cache"] = total["missing_cost"] = total["missing_usage"] = True
            continue
        usage = (data.get("result") or {}).get("usage") or {}
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}
        read = details.get("cached_tokens", usage.get("cache_read_input_tokens"))
        write = details.get("cache_write_tokens", usage.get("cache_creation_input_tokens"))
        if usage.get("input_tokens", usage.get("prompt_tokens")) is None or usage.get("output_tokens", usage.get("completion_tokens")) is None:
            total["missing_usage"] = True
        input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
        total["input"] += input_tokens
        total["cache_read"] += int(read or 0)
        total["cache_write"] += int(write or 0)
        if read is None and write is None:
            total["missing_cache"] = True
            total["unclassified"] += input_tokens
        cost = usage.get("cost")
        if not isinstance(cost, (int, float)):
            cost = usage.get("response_cost")
        if isinstance(cost, (int, float)):
            total["cost"] += cost
            total["cost_calls"] += 1
        else:
            total["missing_cost"] = True
    return total


def _aggregate_message_usage(work_dir: Path) -> tuple[int, int, float]:
    """Fallback: sum (input, output, cost) once per assistant message."""
    sessions_root = work_dir / _SESSIONS_SUBDIR
    if not sessions_root.is_dir():
        return 0, 0, 0.0
    in_t = out_t = 0
    cost = 0.0
    for path in sorted(sessions_root.glob("*/transcript.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "message":
                continue
            msg = entry.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            usage = msg.get("usage") or {}
            in_t += int(usage.get("input") or 0)
            out_t += int(usage.get("output") or 0)
            c = usage.get("cost")
            if c:
                cost += float(c)
    return in_t, out_t, cost
