"""OpenClaw transcript timestamps survive post-run trajectory import."""

from ale_run.agents.openclaw_cli.deployer import OpenClawCliDeployer
from ale_run.base_interface.trajectory import TrajectoryBuilder


def test_session_event_preserves_assistant_and_tool_timestamps():
    builder = TrajectoryBuilder(agent_name="openclaw-cli", task_path="demo", variant_index=0)
    events = [
        {
            "type": "message", "timestamp": "2026-10-20T22:56:38.656Z",
            "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "call-1", "name": "read", "arguments": {}}]},
        },
        {
            "type": "message", "timestamp": "2026-10-20T22:56:38.699Z",
            "message": {"role": "toolResult", "toolCallId": "call-1", "content": [{"type": "text", "text": "ok"}]},
        },
        {
            "type": "message", "message": {"role": "assistant", "timestamp": 1792536999000,
                "content": [{"type": "text", "text": "done"}]},
        },
    ]
    for event in events:
        OpenClawCliDeployer._consume_session_event(event, builder)
    assert [step.timestamp for step in builder.trajectory.steps] == [
        "2026-10-20T22:56:38.656000Z", "2026-10-20T22:56:38.699000Z", "2026-10-20T22:56:39Z",
    ]
