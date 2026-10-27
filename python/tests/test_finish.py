# Modified for Norma.
"""Public SDK finish recorder and runtime check contracts."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from unittest.mock import patch

import httpx
import pytest

from norma.checkpoints import RuleViolationError
from norma.checkpoints import _checkpoint_decorator as rule
from norma.finish import TurnRecorder, check_finish
from norma.models import ActionSummary, ContinuationState, FinishVerdict


def _recorder() -> TurnRecorder:
    return TurnRecorder(session_id="session-1", turn_id="turn-1", user_request="Run tests")


def test_recorder_keeps_recent_redacted_evidence_and_independent_file_list() -> None:
    recorder = _recorder()
    secret = "Bearer abcdefghijklmnopqrstuvwxyz"
    for index in range(51):
        recorder.record_action(
            kind="edit",
            native_tool="edit",
            target=f"src/file{index}.py",
            status="succeeded",
            output_tail="x" * 1000 + secret,
        )

    turn = recorder.turn("Done")

    assert len(turn.actions) == 50
    assert turn.actions_truncated
    assert turn.actions[0].index == 1
    assert len(turn.files_touched) == 51
    assert secret not in str(turn.model_dump())
    assert "Bearer [REDACTED]" in turn.actions[-1].output_tail


def test_only_successful_edit_counts_as_file_touched() -> None:
    recorder = _recorder()
    recorder.record_action(kind="edit", native_tool="edit", target="bad.py", status="failed")
    recorder.record_action(kind="edit", native_tool="edit", target="denied.py", status="denied")
    recorder.record_action(kind="edit", native_tool="edit", target="good.py", status="succeeded")
    assert recorder.turn("Done").files_touched == ["good.py"]


def test_recorder_snapshots_cannot_change_recorded_action_evidence() -> None:
    # Given: a failed action recorded through the public API.
    recorder = _recorder()
    returned_action = recorder.record_action(
        kind="shell", native_tool="tests", command="pytest", status="failed", exit_code=1
    )

    # When: a caller edits both the return value and a previously built turn.
    returned_action.status = "succeeded"
    first_turn = recorder.turn("Tests failed")
    first_turn.actions[0].exit_code = 0

    # Then: subsequent finishes retain the actual failed action.
    action = recorder.turn("Tests still failed").actions[0]
    assert action.status == "failed"
    assert action.exit_code == 1


@pytest.mark.asyncio
async def test_decorated_calls_record_denied_failed_success_and_post_denied() -> None:
    recorder = _recorder()
    agent = type("Agent", (), {"agent_name": "test-agent"})()
    safe = {"allowed": True, "confidence": 1.0, "matches": []}
    denied = {
        "allowed": False,
        "confidence": 1.0,
        "matches": [
            {
                "rule_id": 1,
                "rule_name": "block",
                "action": "deny",
                "result": {"matched": True, "message": "blocked"},
            }
        ],
    }
    calls = 0

    @rule(step_type="tool")
    async def tool(mode: str) -> object:
        nonlocal calls
        calls += 1
        if mode == "raise":
            raise RuntimeError("tool failed")
        if mode == "nonzero":
            return subprocess.CompletedProcess(["pytest"], 1, stdout="failed")
        return "worked"

    with (
        recorder,
        patch("norma.checkpoints._get_current_agent", return_value=agent),
        patch(
            "norma.checkpoints._evaluate",
            side_effect=[
                denied,
                safe,
                safe,
                safe,
                safe,
                denied,
            ],
        ),
    ):
        with pytest.raises(RuleViolationError):
            await tool("denied")
        with pytest.raises(RuntimeError):
            await tool("raise")
        await tool("nonzero")
        with pytest.raises(RuleViolationError):
            await tool("post-denied")

    actions = recorder.turn("All done").actions
    assert calls == 3
    assert [action.status for action in actions] == ["denied", "failed", "failed", "succeeded"]
    assert actions[2].exit_code == 1


@pytest.mark.asyncio
async def test_nested_and_concurrent_recorders_do_not_mix() -> None:
    outer = TurnRecorder(session_id="outer", turn_id="turn")
    inner = TurnRecorder(session_id="inner", turn_id="turn")
    with outer:
        outer.record_action(kind="other", native_tool="outer", status="succeeded")
        with inner:
            inner.record_action(kind="other", native_tool="inner", status="failed")
        outer.record_action(kind="other", native_tool="outer2", status="succeeded")

    async def one(name: str) -> list[ActionSummary]:
        recorder = TurnRecorder(session_id=name, turn_id="turn")
        with recorder:
            await asyncio.sleep(0)
            recorder.record_action(kind="other", native_tool=name, status="succeeded")
        return recorder.turn("Done").actions

    first, second = await asyncio.gather(one("a"), one("b"))
    assert [action.native_tool for action in outer.turn("Done").actions] == ["outer", "outer2"]
    assert [action.native_tool for action in inner.turn("Done").actions] == ["inner"]
    assert first[0].native_tool == "a"
    assert second[0].native_tool == "b"


@pytest.mark.asyncio
async def test_tool_payload_and_recorder_share_explicit_normalized_action() -> None:
    recorder = _recorder()
    agent = type("Agent", (), {"agent_name": "test-agent"})()
    payloads: list[dict[str, object]] = []

    async def evaluate(*args: object, **kwargs: object) -> dict[str, object]:
        payloads.append(args[1])
        return {"allowed": True, "confidence": 1.0, "matches": []}

    @rule(
        step_type="tool",
        action_kind="shell",
        action_target_arg="target",
        action_command_arg="command",
        action_cwd_arg="cwd",
    )
    async def run(command: str, target: str, cwd: str) -> dict[str, int]:
        return {"exit_code": 1}

    with (
        recorder,
        patch("norma.checkpoints._get_current_agent", return_value=agent),
        patch("norma.checkpoints._evaluate", side_effect=evaluate),
    ):
        await run("pytest tests", "tests", "/workspace")

    expected = {"kind": "shell", "target": "tests", "command": "pytest tests", "cwd": "/workspace"}
    assert [payload["context"]["norma_action"] for payload in payloads] == [expected, expected]
    action = recorder.turn("Done").actions[0]
    assert (action.kind, action.command, action.target, action.status) == (
        "shell",
        "pytest tests",
        "tests",
        "failed",
    )


@pytest.mark.asyncio
async def test_shell_without_explicit_target_keeps_target_unknown() -> None:
    recorder = _recorder()
    agent = type("Agent", (), {"agent_name": "test-agent"})()
    payloads: list[dict[str, object]] = []

    async def evaluate(*args: object, **kwargs: object) -> dict[str, object]:
        payloads.append(args[1])
        return {"allowed": True, "confidence": 1.0, "matches": []}

    @rule(step_type="tool", action_kind="shell", action_command_arg="command")
    async def run_shell(command: str) -> str:
        return "ok"

    with (
        recorder,
        patch("norma.checkpoints._get_current_agent", return_value=agent),
        patch("norma.checkpoints._evaluate", side_effect=evaluate),
    ):
        await run_shell("pytest")
    assert all(payload["context"]["norma_action"]["target"] is None for payload in payloads)
    assert recorder.turn("Done").actions[0].target is None


@pytest.mark.asyncio
async def test_nested_rule_denial_records_outer_executed_action_as_failed() -> None:
    recorder = _recorder()
    agent = type("Agent", (), {"agent_name": "test-agent"})()
    safe = {"allowed": True, "confidence": 1.0, "matches": []}

    @rule(step_type="tool")
    async def outer() -> None:
        raise RuleViolationError(message="inner action denied")

    with (
        recorder,
        patch("norma.checkpoints._get_current_agent", return_value=agent),
        patch("norma.checkpoints._evaluate", return_value=safe),
    ):
        with pytest.raises(RuleViolationError):
            await outer()
    assert recorder.turn("Done").actions[0].status == "failed"


@pytest.mark.asyncio
async def test_explicit_mcp_is_error_marks_returned_action_failed() -> None:
    recorder = _recorder()
    agent = type("Agent", (), {"agent_name": "test-agent"})()
    safe = {"allowed": True, "confidence": 1.0, "matches": []}

    @rule(step_type="tool")
    async def call_mcp() -> dict[str, object]:
        return {"isError": True, "content": "failed"}

    with (
        recorder,
        patch("norma.checkpoints._get_current_agent", return_value=agent),
        patch("norma.checkpoints._evaluate", return_value=safe),
    ):
        await call_mcp()
    assert recorder.turn("Done").actions[0].status == "failed"


@pytest.mark.asyncio
async def test_check_finish_posts_exact_turn_and_returns_typed_verdict() -> None:
    turn = _recorder().turn("Tests pass")
    verdict = FinishVerdict(
        verdict="hold",
        reason=(
            "Evidence contradicts tests pass. Fix it, or finish by stating plainly "
            "what is not done."
        ),
        reason_codes=["claim_contradicted"],
        held_by=[7],
        would_hold_by=[],
        continuation=ContinuationState(count=0, max=2),
        allow_finish=False,
        outcome="held",
    )
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(
            200, json={**verdict.model_dump(mode="json"), "execution_contract": "hosted-v1"}
        )

    from norma.client import NormaClient

    async with NormaClient(transport=httpx.MockTransport(handler)) as client:
        actual = await check_finish(turn, client=client, agent_name="test-agent")

    assert actual.model_copy(update={"execution_contract": None}) == verdict
    assert b'"name":"python-sdk.finish"' in captured["body"]
    assert b'"stage":"pre"' in captured["body"]


@pytest.mark.asyncio
async def test_opted_in_finish_requires_ack_and_preserves_frozen_control_evidence() -> None:
    from norma.client import NormaClient

    turn = _recorder().turn("Done")
    payload = {
        "execution_contract": "hosted-v1",
        "control_contract": "rule-controls-v1",
        "agent_generation": "generation-1",
        "control_resolutions": None,
        "control_evidence": [{
            "schema_version": 1,
            "completeness": "unavailable",
            "agent_name": "test-agent",
            "agent_generation": "generation-1",
            "rule_id": 7,
            "rule_version": None,
            "definition_fingerprint": "a" * 64,
            "references": [],
        }],
        "verdict": "pass", "reason": "Done", "reason_codes": [], "held_by": [],
        "would_hold_by": [], "continuation": {"count": 0, "max": 2},
        "allow_finish": True, "outcome": "completed", "provider_input_tokens": None,
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=payload,
            headers={"X-Norma-Control-Contract": "rule-controls-v1"},
        )

    async with NormaClient(
        agent_key="key", control_contract="rule-controls-v1", transport=httpx.MockTransport(handler)
    ) as client:
        verdict = await check_finish(turn, client=client, agent_name="test-agent")

    assert verdict.verdict == "pass"
    assert verdict.control_contract == "rule-controls-v1"
    assert verdict.agent_generation == "generation-1"
    assert verdict.control_evidence[0].completeness == "unavailable"
    assert seen[0].headers["X-Norma-Control-Contract"] == "rule-controls-v1"


@pytest.mark.asyncio
async def test_opted_in_finish_with_missing_ack_is_unverified_without_replay() -> None:
    from norma.client import NormaClient

    turn = _recorder().turn("Done")
    payload = {
        "execution_contract": "hosted-v1", "control_contract": "rule-controls-v1",
        "verdict": "pass", "reason": "Done", "reason_codes": [], "held_by": [],
        "would_hold_by": [], "continuation": {"count": 0, "max": 2},
        "allow_finish": True, "outcome": "completed", "provider_input_tokens": None,
    }
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload)

    async with NormaClient(
        agent_key="key", control_contract="rule-controls-v1", transport=httpx.MockTransport(handler)
    ) as client:
        verdict = await check_finish(turn, client=client, agent_name="test-agent")

    assert calls == 1
    assert (verdict.verdict, verdict.allow_finish, verdict.outcome) == (
        "unavailable", True, "finished_unverified"
    )


@pytest.mark.asyncio
async def test_check_finish_preserves_explicit_target_context() -> None:
    from norma.client import NormaClient

    turn = _recorder().turn("Done")
    received: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.read())
        return httpx.Response(
            200,
            json=FinishVerdict(
                execution_contract="hosted-v1",
                verdict="pass",
                reason="Done",
                reason_codes=[],
                held_by=[],
                would_hold_by=[],
                continuation=ContinuationState(count=0, max=2),
                allow_finish=True,
                outcome="completed",
            ).model_dump(mode="json"),
        )

    async with NormaClient(transport=httpx.MockTransport(handler)) as client:
        verdict = await check_finish(
            turn,
            client=client,
            agent_name="test-agent",
            target_type="workspace",
            target_id="workspace-1",
        )
    assert verdict.verdict == "pass"
    assert b'"target_type":"workspace"' in received[0]
    assert b'"target_id":"workspace-1"' in received[0]


@pytest.mark.asyncio
async def test_check_finish_fails_open_on_network_and_malformed_verdict() -> None:
    from norma.client import NormaClient

    turn = _recorder().turn("Done")
    for response in [httpx.Response(503), httpx.Response(200, json={"verdict": "hold"})]:
        async with NormaClient(transport=httpx.MockTransport(lambda _: response)) as client:
            verdict = await check_finish(turn, client=client, agent_name="test-agent")
            assert verdict.verdict == "unavailable"
            assert verdict.allow_finish


@pytest.mark.asyncio
async def test_check_finish_revalidates_and_redacts_mutated_turn_before_send() -> None:
    from norma.client import NormaClient

    raw_secret = "Bearer abcdefghijklmnopqrstuvwxyz"
    turn = _recorder().turn("Done").model_copy(update={"final_message": raw_secret})
    received: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.read())
        return httpx.Response(
            200,
            json=FinishVerdict(
                execution_contract="hosted-v1",
                verdict="pass",
                reason="Done",
                reason_codes=[],
                held_by=[],
                would_hold_by=[],
                continuation=ContinuationState(count=0, max=2),
                allow_finish=True,
                outcome="completed",
            ).model_dump(mode="json"),
        )

    async with NormaClient(transport=httpx.MockTransport(handler)) as client:
        assert (await check_finish(turn, client=client, agent_name="test-agent")).verdict == "pass"
        malformed = turn.model_copy(update={"final_message": "x" * 9000})
        assert (await check_finish(malformed, client=client, agent_name="test-agent")).verdict == (
            "unavailable"
        )
    assert len(received) == 1
    assert raw_secret.encode() not in received[0]
    assert b"Bearer [REDACTED]" in received[0]


def test_base_sdk_import_does_not_require_framework_extras() -> None:
    script = """
import sys
class BlockOptional:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pydantic_ai" or fullname.startswith("pydantic_ai."):
            raise ImportError("pydantic-ai optional dependency blocked")
        if fullname == "agents" or fullname.startswith("agents."):
            raise ImportError("openai-agents optional dependency blocked")
sys.meta_path.insert(0, BlockOptional())
import norma
from norma.finish import TurnRecorder, check_finish
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_multibyte_turn_over_wire_limit_fails_open_without_post() -> None:
    from norma.client import NormaClient

    recorder = _recorder()
    for _ in range(50):
        recorder.record_action(
            kind="other", native_tool="text", status="succeeded", output_tail="字" * 1000
        )
    turn = recorder.turn("Done")
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(500)

    async with NormaClient(transport=httpx.MockTransport(handler)) as client:
        verdict = await check_finish(turn, client=client, agent_name="test-agent")
    assert verdict.verdict == "unavailable"
    assert posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_finish_http_failure_is_unverified_not_a_pass(status: int) -> None:
    from norma.client import NormaClient

    turn = _recorder().turn("Done")
    async with NormaClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status))
    ) as client:
        verdict = await check_finish(turn, client=client, agent_name="test-agent")
    assert verdict == FinishVerdict(
        verdict="unavailable",
        reason="Finish allowed: the rule service could not complete this check.",
        reason_codes=["unavailable"], held_by=[], would_hold_by=[],
        continuation=turn.continuation, allow_finish=True,
        outcome="finished_unverified", provider_input_tokens=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
async def test_finish_transport_failure_does_not_claim_verified_completion(failure) -> None:
    from norma.client import NormaClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure("synthetic transport failure", request=request)

    async with NormaClient(transport=httpx.MockTransport(handler)) as client:
        verdict = await check_finish(_recorder().turn("Done"), client=client,
                                     agent_name="test-agent")
    assert (verdict.verdict, verdict.allow_finish, verdict.outcome) == (
        "unavailable", True, "finished_unverified",
    )
