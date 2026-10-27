# Modified for Norma.
"""Versioned, frozen Console control receipt and execution evidence."""

from __future__ import annotations

import json
import math
from typing import Literal, Self

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from .base import BaseModel

JSONValue = JsonValue


class _FrozenDict(dict[str, object]):
    """JSON-compatible mapping that rejects ordinary nested mutation."""

    def _blocked(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("control evidence snapshots are immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    clear = _blocked
    pop = _blocked
    setdefault = _blocked
    update = _blocked

    def popitem(self) -> tuple[str, object]:
        self._blocked()
        raise AssertionError("unreachable")

    def __ior__(self, _other: dict[str, object]) -> _FrozenDict:  # type: ignore[override,misc]
        self._blocked()
        return self


class _FrozenList(list[object]):
    """JSON-compatible sequence that rejects ordinary nested mutation."""

    def _blocked(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("control evidence snapshots are immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    append = _blocked
    clear = _blocked
    extend = _blocked
    insert = _blocked
    pop = _blocked
    remove = _blocked
    reverse = _blocked
    sort = _blocked

    def __iadd__(self, _other: list[object]) -> _FrozenList:  # type: ignore[override,misc]
        self._blocked()
        return self

    def __imul__(self, _count: object) -> _FrozenList:  # type: ignore[override,misc]
        self._blocked()
        return self


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        frozen_dict = _FrozenDict()
        dict.__init__(frozen_dict, {key: _freeze_json(item) for key, item in value.items()})
        return frozen_dict
    if isinstance(value, list):
        frozen_list = _FrozenList()
        list.__init__(frozen_list, (_freeze_json(item) for item in value))
        return frozen_list
    return value


def _json_identity(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )



class ControlReceiptReference(BaseModel):
    """One authored control reference and its trusted destination metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    key: str
    source_pointer: str
    destination_pointer: str
    evaluator_pointer: str
    slot: dict[str, JSONValue]

    def model_post_init(self, __context: object) -> None:
        object.__setattr__(self, "slot", _freeze_json(self.slot))


class ControlReceipt(BaseModel):
    """Immutable effective values recorded for one agent/rule revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    contract: Literal["rule-controls-v1"]
    capabilities_fingerprint: str
    declaration_fingerprint: str
    defaults: dict[str, JSONValue]
    effective_values: dict[str, JSONValue]
    values_fingerprint: str
    value_sources: dict[str, Literal["default", "agent_override"]]
    references: list[ControlReceiptReference]
    overridden_keys: list[str]
    agent_generation: str
    agent_name: str
    rule_id: int = Field(ge=1)
    rule_version: int = Field(ge=1)
    override_revision: int = Field(ge=0)
    effective_fingerprint: str

    def model_post_init(self, __context: object) -> None:
        for name in ("defaults", "effective_values", "value_sources"):
            object.__setattr__(self, name, _freeze_json(getattr(self, name)))
        for name in ("references", "overridden_keys"):
            object.__setattr__(self, name, _freeze_json(getattr(self, name)))


class NumericComparisonEvidence(BaseModel):
    """One bounded numeric comparison; observed state may be explicitly redacted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    field_path: str = Field(min_length=1, max_length=256)
    operator: Literal["min", "max"]
    observed: int | float | None
    expected: int | float
    outcome: Literal["passed", "failed", "not_evaluated"]
    observed_state: Literal["redacted"] | None = None
    reason: Literal[
        "field_missing",
        "not_numeric",
        "not_finite",
        "prerequisite_failed",
    ] | None = None

    @field_validator("observed", "expected")
    @classmethod
    def finite_numeric_evidence(cls, value: int | float | None) -> int | float | None:
        if value is None:
            return value
        if type(value) is int:
            if abs(value) > int(float.fromhex("0x1.fffffffffffffp+1023")):
                raise ValueError("numeric comparison evidence must be finite")
            return value
        if type(value) is not float or not math.isfinite(value):
            raise ValueError("numeric comparison evidence must be finite")
        return value

    @model_validator(mode="after")
    def comparison_status_is_honest(self) -> Self:
        if self.observed_state == "redacted":
            if (
                self.observed is not None
                or self.outcome == "not_evaluated"
                or self.reason is not None
            ):
                raise ValueError("redacted observations preserve evaluated outcome without values")
        elif self.outcome == "not_evaluated":
            if self.observed is not None or self.reason is None:
                raise ValueError("not-evaluated comparisons require a reason and no observation")
        elif self.observed is None or self.reason is not None:
            raise ValueError("evaluated comparisons require an observation and no reason")
        return self


class ControlReferenceEvidence(BaseModel):
    """Actual disposition for one frozen controlled destination."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    key: str
    destination_pointer: str
    condition_id: str | None = None
    configured_default: JSONValue
    effective_value: JSONValue
    value_source: Literal["default", "agent_override"]
    disposition: Literal["configured", "evaluated", "skipped", "unavailable"]
    unavailable_reason: Literal[
        "trace_missing",
        "cancelled_without_trace",
        "evaluator_error",
        "comparison_not_evaluated",
        "comparison_missing",
    ] | None = None
    numeric_comparisons: list[NumericComparisonEvidence] = Field(
        default_factory=list, max_length=64
    )
    numeric_comparisons_truncated: bool | None = None

    def model_post_init(self, __context: object) -> None:
        object.__setattr__(self, "configured_default", _freeze_json(self.configured_default))
        object.__setattr__(self, "effective_value", _freeze_json(self.effective_value))
        object.__setattr__(self, "numeric_comparisons", _freeze_json(self.numeric_comparisons))


class ControlEvidenceSnapshot(BaseModel):
    """Self-contained evidence from a check or replay, never reconstructed live."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1]
    completeness: Literal["complete", "incomplete", "unavailable"]
    agent_name: str
    agent_generation: str
    rule_id: int = Field(ge=1)
    rule_version: int | None = Field(default=None, ge=1)
    definition_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    concrete_definition_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    effective_definition: dict[str, JSONValue] | None = None
    receipt: ControlReceipt | None = None
    references: list[ControlReferenceEvidence] = Field(default_factory=list, max_length=256)
    numeric_comparisons_truncated: bool | None = None

    @model_validator(mode="after")
    def consistent_receipt(self) -> ControlEvidenceSnapshot:
        if self.receipt is not None:
            if (
                self.receipt.agent_name != self.agent_name
                or self.receipt.agent_generation != self.agent_generation
                or self.receipt.rule_id != self.rule_id
                or self.receipt.rule_version != self.rule_version
                or self.definition_fingerprint != self.receipt.effective_fingerprint
            ):
                raise ValueError("control evidence identity does not match its receipt")
        if self.completeness == "complete" and (
            self.rule_version is None or self.receipt is None or self.effective_definition is None
            or self.concrete_definition_fingerprint is None
        ):
            raise ValueError(
                "complete control evidence requires frozen definition fingerprints and receipt"
            )
        if self.completeness == "unavailable" and self.references:
            raise ValueError("unavailable control evidence cannot assert dispositions")
        if sum(len(item.numeric_comparisons) for item in self.references) > 64:
            raise ValueError("control evidence exceeds its numeric comparison limit")
        if self.receipt is None and self.references:
            raise ValueError("control evidence references require a receipt")
        if self.receipt is not None:
            receipt_refs = {
                (item.key, item.destination_pointer): item
                for item in self.receipt.references
            }
            evidence_refs = {
                (item.key, item.destination_pointer): item for item in self.references
            }
            if len(receipt_refs) != len(self.receipt.references):
                raise ValueError("control receipt references must be unique")
            if len(evidence_refs) != len(self.references):
                raise ValueError("control evidence references must be unique")
            if self.completeness == "complete" and evidence_refs.keys() != receipt_refs.keys():
                raise ValueError("complete control evidence references do not match the receipt")
            if not evidence_refs.keys() <= receipt_refs.keys():
                raise ValueError("control evidence references must be present in its receipt")
            for identity, evidence in evidence_refs.items():
                key = receipt_refs[identity].key
                if (
                    key not in self.receipt.defaults
                    or key not in self.receipt.effective_values
                    or key not in self.receipt.value_sources
                ):
                    raise ValueError("control receipt is missing a referenced value")
                if (
                    _json_identity(evidence.configured_default)
                    != _json_identity(self.receipt.defaults[key])
                    or _json_identity(evidence.effective_value)
                    != _json_identity(self.receipt.effective_values[key])
                    or evidence.value_source != self.receipt.value_sources[key]
                ):
                    raise ValueError("control evidence values must match its receipt")
        for name in ("effective_definition",):
            object.__setattr__(self, name, _freeze_json(getattr(self, name)))
        object.__setattr__(self, "references", _freeze_json(self.references))
        return self
