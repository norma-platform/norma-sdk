# Modified for Norma.
"""OpenAI Agents output-guardrail integration for Norma finish rules.

Install with ``norma-sdk[openai-agents]``. Only this helper's own
tripwire is caught. An in-memory session retains completed tools and the
blocked-output marker across a hold; the next run sends only Norma's reason.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from agents import (
    Agent,
    GuardrailFunctionOutput,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    RunResult,
    SQLiteSession,
    output_guardrail,
)
from agents.memory.session import Session

from norma.client import NormaClient
from norma.finish import TurnRecorder, _unavailable, check_finish
from norma.models import FinishVerdict, TurnRecord

_ORIGINAL_RUN = Runner.run
_finish_clone: ContextVar[tuple[Agent[Any], Agent[Any]] | None] = ContextVar(
    "norma_openai_finish_clone", default=None
)


@contextmanager
def _finish_clone_alias(clone: Agent[Any], original: Agent[Any]) -> Iterator[None]:
    token = _finish_clone.set((clone, original))
    try:
        yield
    finally:
        _finish_clone.reset(token)


def _original_agent_for_finish_clone(agent: Agent[Any]) -> Agent[Any]:
    alias = _finish_clone.get()
    if alias is not None and alias[0] is agent:
        return alias[1]
    return agent


@dataclass(frozen=True)
class OpenAIFinishResult:
    """A completed framework run and its final Norma finish verdict."""

    result: RunResult
    verdict: FinishVerdict
    turn: TurnRecord


async def run_openai_agents_with_finish[TContext](
    agent: Agent[TContext],
    prompt: str,
    recorder: TurnRecorder,
    *,
    client: NormaClient | None = None,
    agent_name: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    session: Session | None = None,
    context: TContext | None = None,
    runner_options: Mapping[str, Any] | None = None,
    ensure_active: Callable[[], None] | None = None,
) -> OpenAIFinishResult:
    """Continue a held text-output turn without repeating completed tools.

    If no session is supplied, this helper owns an in-memory session that is
    discarded after the turn. A supplied session may preserve earlier history.
    Unrelated guardrail tripwires and agent errors propagate to the caller.
    """
    count = 0
    agent_generation: str | None = None
    verdict: FinishVerdict | None = None
    last_turn: TurnRecord | None = None
    owned_session = SQLiteSession(recorder.session_id) if session is None else None
    active_session = session or owned_session
    assert active_session is not None
    forwarded_options = dict(runner_options or {})
    forwarded_options.pop("context", None)
    forwarded_options.pop("session", None)

    @output_guardrail(name="norma_finish")
    async def norma_finish(
        ctx: RunContextWrapper[TContext],
        checked_agent: Agent[TContext],
        output: Any,
    ) -> GuardrailFunctionOutput:
        nonlocal verdict, last_turn, agent_generation
        _ = (ctx, checked_agent)
        turn = recorder.turn(
            output if isinstance(output, str) else "",
            continuation_count=count,
            agent_generation=agent_generation,
        )
        last_turn = turn
        if not isinstance(output, str):
            verdict = _unavailable(turn.continuation)
        else:
            if ensure_active is not None:
                ensure_active()
            verdict = await check_finish(
                turn,
                client=client,
                agent_name=agent_name,
                target_type=target_type,
                target_id=target_id,
            )
            agent_generation = verdict.continuation.agent_generation
            if ensure_active is not None:
                ensure_active()
        return GuardrailFunctionOutput(
            output_info=verdict, tripwire_triggered=verdict.verdict == "hold"
        )

    guarded_agent = agent.clone(output_guardrails=[*agent.output_guardrails, norma_finish])
    next_input = prompt
    try:
        with _finish_clone_alias(guarded_agent, agent):
            from norma.finish import use_turn_recorder

            with use_turn_recorder(recorder):
                while True:
                    verdict = None
                    try:
                        if ensure_active is not None:
                            ensure_active()
                        result = await _ORIGINAL_RUN(
                            guarded_agent,
                            next_input,
                            context=context,
                            session=active_session,
                            **forwarded_options,
                        )
                        return OpenAIFinishResult(
                            result=result,
                            verdict=verdict
                            or _unavailable(
                                recorder.turn(
                                    "",
                                    continuation_count=count,
                                    agent_generation=agent_generation,
                                ).continuation,
                                "Finish allowed unverified: the final agent did not run "
                                "Norma's output guardrail.",
                            ),
                            turn=last_turn
                            or recorder.turn(
                                "",
                                continuation_count=count,
                                agent_generation=agent_generation,
                            ),
                        )
                    except OutputGuardrailTripwireTriggered as exc:
                        if exc.guardrail_result.guardrail is not norma_finish:
                            raise
                        assert verdict is not None and verdict.verdict == "hold"
                        count += 1
                        next_input = verdict.reason
    finally:
        if owned_session is not None:
            owned_session.close()
