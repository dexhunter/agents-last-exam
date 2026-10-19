"""Map retained Muse events to ALE messages, tools, images, and accounting."""

from __future__ import annotations

import json
from datetime import UTC, datetime
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

from .telemetry import native_events, read_records


def parse_trajectory(work_dir: Path, builder: TrajectoryBuilder) -> None:
    root_id = None
    terminal = None
    for record in read_records(work_dir / "transcript.jsonl"):
        root_id = root_id or (record.get("stream") or {}).get("id")
        if str(record.get("payload_type", "")).startswith("run.terminal."):
            terminal = record.get("payload")

    children: dict[str, TrajectoryBuilder] = {}
    results: dict[tuple[str, str], ToolResult] = {}
    pending: dict[tuple[str, str], Any] = {}
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    task_calls: dict[tuple[str, str], str] = {}
    failed_calls: set[tuple[str, str]] = set()
    for event in native_events(work_dir):
        identity = (event.get("session_id", ""), event.get("task_id", ""))
        if event.get("kind") == "side_effect_intent" and event.get("operation", "").startswith(
            "tool:"
        ):
            task_calls[identity] = event.get("idempotency_key", "").removeprefix("tool:")
        elif (
            event.get("lane") == "task" and event.get("kind") == "failed" and identity in task_calls
        ):
            failed_calls.add((identity[0], task_calls[identity]))
    found = False
    for event in native_events(work_dir):
        found = True
        session_id = event.get("session_id", "")
        root_id = root_id or session_id
        target = builder
        if session_id != root_id:
            if session_id not in children:
                children[session_id] = TrajectoryBuilder(
                    episode_id=session_id,
                    agent_name="muse-code",
                    agent_version=builder.trajectory.agent.version,
                    model=builder.trajectory.agent.model,
                    task_path=builder.trajectory.task_path,
                    variant_index=builder.trajectory.variant_index,
                )
            target = children[session_id]
        kind = event.get("kind")
        extra = {
            "muse_event": kind,
            "session_id": session_id,
            "run_id": event.get("run_id"),
            "record_id": event.get("record_id"),
        }
        before = len(target.trajectory.steps)
        if kind == "assistant_message_committed":
            target.add_step(source="agent", message=event.get("text", ""), extra=extra)
        elif kind in {"reasoning_summary_committed", "reasoning_committed"}:
            target.add_step(
                source="agent",
                reasoning=event.get("text", ""),
                extra={
                    **extra,
                    "response_id": event.get("response_id"),
                    "provider_item_id": event.get("provider_item_id"),
                    "encrypted": bool(event.get("encrypted_content")),
                },
            )
        elif kind == "assistant_tool_calls_committed":
            calls = []
            for call in event.get("tool_calls", []):
                args = call.get("args", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"raw": args}
                if not isinstance(args, dict):
                    args = {"value": args}
                calls.append(
                    ToolCall(
                        id=call.get("call_id") or call.get("id") or "unknown",
                        name=call.get("name") or "unknown",
                        arguments=args,
                    )
                )
            step = target.add_step(source="agent", tool_calls=calls, extra=extra)
            for call in calls:
                pending[(session_id, call.id)] = step
        elif kind == "tool_result_batch_committed":
            observations = []
            for result in event.get("results", []):
                call_id = result.get("tool_call_id", "")
                item = ToolResult(
                    tool_call_id=call_id,
                    content=[ContentPart(type="text", text=result.get("text", ""))],
                    is_error=bool(result.get("is_error", False))
                    or (session_id, call_id) in failed_calls,
                )
                observations.append(item)
                results[(session_id, call_id)] = item
                pending.pop((session_id, call_id), None)
            target.add_step(
                source="environment", observation=Observation(results=observations), extra=extra
            )
        elif kind == "tool_result_model_visible_content":
            call_id = event.get("call_id", "")
            parts = []
            for content in event.get("content", []):
                if content.get("kind") == "image" and content.get("base64_data"):
                    parts.append(
                        ContentPart(
                            type="image",
                            image=ImageSource(
                                type="base64",
                                data=content["base64_data"],
                                media_type=content.get("media_type", "image/png"),
                            ),
                        )
                    )
                elif content.get("kind") == "text":
                    parts.append(ContentPart(type="text", text=content.get("text", "")))
            item = results.get((session_id, call_id))
            if item is not None:
                item.content.extend(parts)
            elif parts:
                item = ToolResult(tool_call_id=call_id, content=parts)
                results[(session_id, call_id)] = item
                target.add_step(
                    source="environment", observation=Observation(results=[item]), extra=extra
                )
        elif kind == "model_completed":
            usage = event.get("usage") or {}
            metrics = StepMetrics(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_read_tokens=usage.get("cache_read_tokens", usage.get("cached_tokens")),
                cache_creation_tokens=usage.get("cache_write_tokens"),
                duration_ms=event.get("duration_ms"),
            )
            for name in totals:
                totals[name] += getattr(metrics, name) or 0
            target.add_step(source="agent", metrics=metrics, extra={**extra, "usage": usage})
        elif kind == "terminal" and event.get("lane") == "run":
            target.trajectory.extra["muse_terminal"] = event
            if event.get("terminal") != "completed":
                target.add_step(
                    source="system", message=event.get("reason") or str(kind), extra=extra
                )
        elif kind == "model_configured":
            target.trajectory.agent.model = event.get("model_id") or target.trajectory.agent.model
        elif kind and "compact" in kind:
            target.add_step(source="system", message=kind, extra={**extra, "event": event})
        elif kind == "failed" and event.get("lane") == "task":
            target.add_step(
                source="system", message=event.get("reason", "task failed"), extra=extra
            )
        timestamp = event.get("recorded_at_us")
        if isinstance(timestamp, (int, float)):
            stamp = datetime.fromtimestamp(timestamp / 1_000_000, UTC).isoformat()
            for step in target.trajectory.steps[before:]:
                step.timestamp = stamp

    for step in pending.values():
        step.extra["status"] = "incomplete"
    for child in children.values():
        end = child.trajectory.extra.get("muse_terminal", {})
        child.finalize(
            reward=None, status="completed" if end.get("terminal") == "completed" else "failed"
        )
        builder.trajectory.subagent_trajectories.append(child.trajectory)
    if found:
        builder.override_final_metrics(**{"total_" + name: value for name, value in totals.items()})
    else:
        builder.add_step(
            source="system",
            message="muse_code: native session log unavailable",
            extra={"reason": "no_native_events"},
        )
        if terminal and terminal.get("text"):
            builder.add_step(source="agent", message=terminal["text"])
    if terminal:
        builder.trajectory.extra["muse_stdout_terminal"] = terminal
