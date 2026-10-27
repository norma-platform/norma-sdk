# Modified for Norma.
"""Agent registration/read response DTOs."""

from typing import Literal

from .agent import Agent, StepSchema
from .base import BaseModel
from .control_evidence import ControlReceipt
from .rules import RuleDefinition


class GetAgentResponse(BaseModel):
    agent: Agent
    steps: list[StepSchema]


class RuleRecord(BaseModel):
    id: int
    name: str
    definition: RuleDefinition | None
    status: Literal["hosted", "unsupported"]


class AgentRulesResponse(BaseModel):
    rules: list[RuleRecord]
    control_contract: Literal["rule-controls-v1"] | None = None
    agent_generation: str | None = None
    control_resolutions: list[ControlReceipt] | None = None
