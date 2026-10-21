"""Frozen, non-secret policy constants carried with published Rule definitions."""

import hashlib
import json
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from .agent import JSONValue
from .base import BaseModel


def parameter_fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


class ParameterBinding(BaseModel):
    """Server-resolved revision, copied into every linked Rule; never SDK input."""

    model_config = ConfigDict(extra="forbid")
    contract: Literal["policy-parameters-v1"] = "policy-parameters-v1"
    set: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    revision: int = Field(ge=1)
    schema_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    values_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    values: dict[str, JSONValue]

    @model_validator(mode="after")
    def verify_values(self) -> Self:
        if len(json.dumps(self.values, allow_nan=False).encode()) > 16_384:
            raise ValueError("Policy parameters exceed 16 KiB")
        if parameter_fingerprint(self.values) != self.values_fingerprint:
            raise ValueError("Policy parameter fingerprint mismatch")
        return self
