# Modified for Norma.
"""OpenAI Agents output-guardrail integration for Norma finish rules.

Install with ``norma-sdk[openai-agents]``. Only this helper's own
tripwire is caught. An in-memory session retains completed tools and the
blocked-output marker across a hold; the next run sends only Norma's reason.
"""

from __future__ import annotations

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
from norma.models import FinishVerdict


@dataclass(frozen=True)
class OpenAIFinishResult:
    """A completed framework run and its final Norma finish verdict."""

    result: RunResult
    verdict: FinishVerdict


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
) -> OpenAIFinishResult:
    """Continue a held text-output turn without repeating completed tools.

    If no session is supplied, this helper owns an in-memory session that is
    discarded after the turn. A supplied session may preserve earlier history.
    Unrelated guardrail tripwires and agent errors propagate to the caller.
    """
    count = 0
    verdict: FinishVerdict | None = None
    owned_session = SQLiteSession(recorder.session_id) if session is None else None
    active_session = session or owned_session
    assert active_session is not None

    @output_guardrail(name="norma_finish")
    async def norma_finish(
        ctx: RunContextWrapper[TContext],
        checked_agent: Agent[TContext],
        output: Any,
    ) -> GuardrailFunctionOutput:
        nonlocal verdict
        _ = (ctx, checked_agent)
        if not isinstance(output, str):
            verdict = _unavailable(recorder.turn("", continuation_count=count).continuation)
        else:
            verdict = await check_finish(
                recorder.turn(output, continuation_count=count),
                client=client,
                agent_name=agent_name,
                target_type=target_type,
                target_id=target_id,
            )
        return GuardrailFunctionOutput(
            output_info=verdict, tripwire_triggered=verdict.verdict == "hold"
        )

    guarded_agent = agent.clone(output_guardrails=[*agent.output_guardrails, norma_finish])
    next_input = prompt
    try:
        with recorder:
            while True:
                verdict = None
                try:
                    result = await Runner.run(
                        guarded_agent, next_input, context=context, session=active_session
                    )
                    return OpenAIFinishResult(
                        result=result,
                        verdict=verdict
                        or _unavailable(
                            recorder.turn("", continuation_count=count).continuation,
                            "Finish allowed unverified: the final agent did not run "
                            "Norma's output guardrail.",
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
