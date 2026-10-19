"""Incrementally mirror Qwen's concatenated native OpenTelemetry JSON objects."""

from __future__ import annotations

import codecs
import json
import os
from collections import Counter
from pathlib import Path

from ale_run.agents._cli_runtime import redact


class TelemetryMirror:
    def __init__(self, source: Path, target: Path, secrets: tuple[str, ...]):
        self.source = source
        self.target = target
        self.secrets = secrets
        self.offset = 0
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")()

    def poll(self) -> None:
        if not self.source.exists():
            return
        with self.source.open("rb") as handle:
            handle.seek(self.offset)
            self.pending += self.decoder.decode(handle.read())
            self.offset = handle.tell()
        decoder = json.JSONDecoder()
        with self.target.open("a", encoding="utf-8") as handle:
            while self.pending.strip():
                self.pending = self.pending.lstrip()
                try:
                    event, end = decoder.raw_decode(self.pending)
                except ValueError:
                    break
                handle.write(json.dumps(redact(event, self.secrets), ensure_ascii=False) + "\n")
                self.pending = self.pending[end:]


def summarize_telemetry(work_dir: Path) -> dict:
    counts = Counter()
    records = 0
    source = work_dir / "telemetry.jsonl"
    if source.exists():
        with source.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                records += 1
                attributes = event.get("attributes", {})
                if name := attributes.get("event.name"):
                    counts[name] += 1
    summary = {
        "source": "Qwen Code native OpenTelemetry file exporter",
        "records": records,
        "events": dict(counts),
    }
    (work_dir / "telemetry_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary


class NativeChatMirror:
    """Copy complete native chat records, including images omitted from stdout."""

    def __init__(self, root: Path, target: Path, secrets: tuple[str, ...]):
        if os.name == "nt":
            absolute = str(root.resolve())
            if not absolute.startswith("\\\\?\\"):
                absolute = (
                    "\\\\?\\UNC\\" + absolute[2:]
                    if absolute.startswith("\\\\")
                    else "\\\\?\\" + absolute
                )
            root = Path(absolute)
        self.root = root
        self.target = target
        self.secrets = secrets
        self.offsets: dict[Path, int] = {}

    def poll(self) -> None:
        with self.target.open("a", encoding="utf-8") as output:
            for source in sorted(self.root.glob("projects/*/chats/**/*.jsonl")):
                with source.open("rb") as handle:
                    handle.seek(self.offsets.get(source, 0))
                    while line := handle.readline():
                        if not line.endswith(b"\n"):
                            break
                        self.offsets[source] = handle.tell()
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        output.write(
                            json.dumps(
                                {
                                    "source": source.relative_to(self.root).as_posix(),
                                    "event": redact(event, self.secrets),
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
