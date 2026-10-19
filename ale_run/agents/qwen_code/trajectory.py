"""Enrich stdout trajectories with Qwen's native images and internal tool events."""

from __future__ import annotations

import json
from pathlib import Path

from ale_run.base_interface import (
    ContentPart,
    ImageSource,
    Observation,
    StepMetrics,
    ToolCall,
    ToolResult,
    TrajectoryBuilder,
)


def records(path: Path):
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event


def enrich_native(work_dir: Path, builder: TrajectoryBuilder) -> dict:
    trajectories = [builder.trajectory, *builder.trajectory.subagent_trajectories]
    calls = {
        call.id: step for traj in trajectories for step in traj.steps for call in step.tool_calls
    }
    results = {
        r.tool_call_id: (step, r)
        for traj in trajectories
        for step in traj.steps
        if step.observation
        for r in step.observation.results
    }
    for record in records(work_dir / "native_chats.jsonl"):
        event = record.get("event", {})
        for part in (event.get("message") or {}).get("parts", []):
            call = part.get("functionCall")
            if call and call.get("id") in calls:
                step = calls[call["id"]]
                if event.get("timestamp"):
                    step.timestamp = event["timestamp"]
                    step.extra["timestamp_source"] = "native_chat"
            response = part.get("functionResponse")
            if not response or response.get("id") not in results:
                continue
            step, result = results[response["id"]]
            if event.get("timestamp"):
                step.timestamp = event["timestamp"]
                step.extra["timestamp_source"] = "native_chat"
            if not any(p.type == "image" for p in result.content):
                for media in response.get("parts", []):
                    data = media.get("inlineData", {})
                    if data.get("data") and data.get("mimeType", "").startswith("image/"):
                        result.content.append(
                            ContentPart(
                                type="image",
                                image=ImageSource(
                                    type="base64",
                                    data=data["data"],
                                    media_type=data["mimeType"],
                                ),
                            )
                        )
            payload = response.get("response", {})
            if payload:
                text = payload.get("output", payload)
                result.content = [
                    ContentPart(
                        type="text",
                        text=text
                        if isinstance(text, str)
                        else json.dumps(text, ensure_ascii=False),
                    )
                ] + [p for p in result.content if p.type == "image"]

    internal = {}
    native_calls = set()
    for record in records(work_dir / "telemetry.jsonl"):
        attributes = record.get("attributes", {})
        event_name = attributes.get("event.name")
        if event_name == "qwen-code.tool_call":
            native_calls.add(attributes.get("call_id"))
        prompt = str(attributes.get("prompt_id", "")).split("#")
        child_id = prompt[1] if len(prompt) > 1 else ""
        if not child_id or event_name not in {"qwen-code.tool_call", "qwen-code.api_response"}:
            continue
        if event_name == "qwen-code.tool_call" and attributes.get("call_id") in calls:
            continue
        if child_id not in internal:
            internal[child_id] = TrajectoryBuilder(
                agent_name="qwen_code",
                task_path=builder.trajectory.task_path,
                variant_index=builder.trajectory.variant_index,
            )
            internal[child_id].trajectory.extra.update(
                {
                    "native_agent_id": child_id,
                    "source": "native_telemetry",
                    "result_content_available": False,
                    "completion_status": "unreported",
                }
            )
        child = internal[child_id]
        if event_name == "qwen-code.api_response":
            step = child.add_step(
                source="agent",
                metrics=StepMetrics(
                    input_tokens=attributes.get("input_token_count"),
                    output_tokens=attributes.get("output_token_count"),
                    cache_read_tokens=attributes.get("cached_content_token_count"),
                    duration_ms=attributes.get("duration_ms"),
                ),
                extra={"response_id": attributes.get("response_id"), "source": "native_telemetry"},
            )
        else:
            args = attributes.get("function_args", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"raw": args}
            call_id = str(attributes.get("call_id") or "")
            step = child.add_step(
                source="agent",
                tool_calls=[
                    ToolCall(
                        id=call_id,
                        name=attributes.get("function_name", "unknown"),
                        arguments=args if isinstance(args, dict) else {},
                    )
                ],
                extra={"source": "native_telemetry"},
            )
            if attributes.get("event.timestamp"):
                step.timestamp = attributes["event.timestamp"]
            step = child.add_step(
                source="environment",
                observation=Observation(
                    results=[
                        ToolResult(
                            tool_call_id=call_id,
                            is_error=not bool(attributes.get("success")),
                        )
                    ]
                ),
                extra={
                    "source": "native_telemetry",
                    "result_content_available": False,
                    "execution_status": attributes.get("execution_status"),
                    "duration_ms": attributes.get("duration_ms"),
                },
            )
        if attributes.get("event.timestamp"):
            step.timestamp = attributes["event.timestamp"]
    for child in internal.values():
        builder.trajectory.subagent_trajectories.append(child.trajectory)
    return {"native_tool_events": len(native_calls), "internal_agents": len(internal)}
