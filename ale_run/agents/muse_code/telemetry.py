"""Retain Muse's native session events, including partial runs and child sessions."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ale_run.agents._cli_runtime import redact


class NativeEventMirror:
    """Append complete native log records to a stable incremental-pull artifact."""

    def __init__(
        self,
        work_dir: Path,
        data_dir: Path,
        secrets: tuple[str, ...] = (),
        *,
        exclude: set[Path] | None = None,
    ):
        self.path = work_dir / "native_events.jsonl"
        self.data_dir = data_dir
        self.secrets = secrets
        self.offsets: dict[Path, int] = {}
        self.exclude = exclude or set()

    def poll(self) -> None:
        with self.path.open("a", encoding="utf-8") as output:
            for path in sorted(self.data_dir.rglob("session.jsonl")):
                if path in self.exclude:
                    continue
                try:
                    with path.open("rb") as source:
                        offset = self.offsets.get(path, 0)
                        if path.stat().st_size < offset:
                            offset = 0
                        source.seek(offset)
                        while line := source.readline():
                            if not line.endswith(b"\n"):
                                break
                            self.offsets[path] = source.tell()
                            text = line.decode("utf-8", errors="replace")
                            output.write(redact(text, self.secrets))
                except FileNotFoundError:
                    continue

    def snapshot(self, destination: Path) -> None:
        """Retain this launch's session evidence without copying application profiles."""
        for source in self.offsets:
            target = destination / source.parent.relative_to(self.data_dir)
            shutil.copytree(source.parent, target, dirs_exist_ok=True)
            log = target / source.name
            log.write_text(
                redact(log.read_text(encoding="utf-8", errors="replace"), self.secrets),
                encoding="utf-8",
            )


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """Read JSONL, tolerating BOMs, malformed records, and truncated final lines."""
    try:
        handle = path.open(encoding="utf-8-sig", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield from _expand_record(record)


def _expand_record(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if record.get("retained_frame"):
        for child in record.get("children", []):
            if not isinstance(child, dict):
                continue
            try:
                decoded = json.loads(child.get("record_json", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(decoded, dict):
                yield from _expand_record(decoded)
    else:
        yield record


def native_events(work_dir: Path) -> Iterator[dict[str, Any]]:
    """Merge incremental and final session logs, deduplicating native record IDs."""
    mirror = work_dir / "native_events.jsonl"
    data_dir = work_dir / "muse-data"
    selection = work_dir / "native_data_path.json"
    if selection.exists():
        relative = Path(json.loads(selection.read_text(encoding="utf-8")))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid Muse native data path")
        data_dir = work_dir / relative
    sources = [mirror, *sorted(data_dir.rglob("session.jsonl"))]
    seen: set[tuple[str, str]] = set()
    for path in sources:
        for record in read_records(path):
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            session_id = (record.get("stream") or {}).get("id", "")
            identity = (session_id, str(record.get("id", "")))
            if identity[1] and identity in seen:
                continue
            seen.add(identity)
            event = payload.get("event")
            if record.get("payload_type") == "runtime.session" and isinstance(event, dict):
                yield {
                    "session_id": session_id,
                    "run_id": payload.get("run_id"),
                    "task_id": payload.get("task_id"),
                    "lane": payload.get("kind"),
                    "record_id": record.get("id"),
                    "recorded_at_us": record.get("recorded_at"),
                    **event,
                }
            elif record.get("payload_type") == "run.model.configured":
                model = payload.get("record", payload)
                yield {
                    "kind": "model_configured",
                    "session_id": session_id,
                    "recorded_at_us": record.get("recorded_at"),
                    **model,
                }


def recover_telemetry_artifacts(work_dir: Path) -> dict[str, Any]:
    """Rebuild telemetry and accounting solely from retained native events."""
    counts: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    calls: list[dict[str, Any]] = []
    provider_events: list[dict[str, Any]] = []
    tools: dict[tuple[str, str], dict[str, Any]] = {}
    terminals: list[dict[str, Any]] = []
    target = work_dir / "telemetry.jsonl"
    temporary = target.with_suffix(".jsonl.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        for event in native_events(work_dir):
            output.write(json.dumps(event, ensure_ascii=False) + "\n")
            kind = event.get("kind", "unknown")
            counts[kind] += 1
            if kind == "model_completed":
                usage = event.get("usage") or {}
                for key in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int):
                        totals[key] += value
                calls.append(event)
            elif event.get("lane") == "task":
                if kind == "status" and any(
                    facet.get("operation") == "model.response"
                    for facet in (event.get("details") or {}).get("facets", [])
                ):
                    provider_events.append(event)
                identity = (event.get("session_id", ""), event.get("task_id", ""))
                if kind == "proposed" and event.get("task_kind", "").startswith("tool."):
                    tools[identity] = {
                        "session_id": identity[0],
                        "task_id": identity[1],
                        "name": event["task_kind"].removeprefix("tool."),
                        "status": "incomplete",
                        "duration_ms": None,
                    }
                tool = tools.get(identity)
                if tool is not None:
                    if kind == "side_effect_intent":
                        tool["call_id"] = event.get("idempotency_key", "").removeprefix("tool:")
                    elif kind == "started":
                        tool["started_at_us"] = event.get("recorded_at_us")
                    elif kind in {"completed", "failed", "cancelled"}:
                        tool["status"] = kind
                        tool["ended_at_us"] = event.get("recorded_at_us")
                        tool["reason"] = event.get("reason")
                        start, end = tool.get("started_at_us"), tool.get("ended_at_us")
                        if isinstance(start, int) and isinstance(end, int) and end >= start:
                            tool["duration_ms"] = (end - start) / 1000
            elif kind == "terminal" and event.get("lane") == "run":
                terminals.append(event)
    temporary.replace(target)
    summary = {
        "source": "muse_native_session_log",
        "events_by_type": dict(counts),
        "event_count": sum(counts.values()),
        "usage": dict(totals),
        "api_calls": calls,
        "provider_events": provider_events,
        "tool_executions": list(tools.values()),
        "terminals": terminals,
        "cost_usd": None,
    }
    (work_dir / "telemetry_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary
