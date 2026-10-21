# Modified for Norma.
"""Agent registration/read response DTOs."""

from typing import Literal

from .agent import Agent, StepSchema
from .base import BaseModel
from .rules import RuleDefinition


class GetAgentResponse(BaseModel):
    agent: Agent
    steps: list[StepSchema]


class RuleRecord(BaseModel):
    id: int
    name: str
    definition: RuleDefinition | None
    status: Literal["hosted", "unsupported", "unrendered"]


class AgentRulesResponse(BaseModel):
    rules: list[RuleRecord]
