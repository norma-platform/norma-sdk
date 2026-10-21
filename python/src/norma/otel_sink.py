# Modified for Norma.
"""OpenTelemetry sink support for Norma Rule-check events."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from norma._telemetry.sinks import BaseRuleEventSink, SinkResult
from norma.models import JSONObject, RuleCheckEvent

from .settings import get_settings
from .tracing import _generate_span_id, validate_span_id, validate_trace_id

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger(__name__)

OTEL_RULE_EVENT_SINK_NAME = "otel"
_OTEL_INSTRUMENTATION_SCOPE = "norma.observability"
_OTEL_NOOP_WARNING = (
    "OpenTelemetry sink selected but OpenTelemetry SDK/exporter dependencies are not available; "
    "Rule events will be dropped; install the Norma OTEL dependencies to export them"
)
_OTEL_EXPORTER_MISSING_WARNING = (
    "OpenTelemetry sink selected but no OTLP exporter configuration was found; "
    "Rule events will not be exported"
)
_DEBUG_METADATA_ATTRIBUTE_KEYS = frozenset(
    {
        "selected_data",
        "selected_data_preview",
        "engine_selected_data",
        "engine_selected_data_preview",
    }
)

AttributeValue = str | bool | int | float | list[str] | list[bool] | list[int] | list[float]


@dataclass(frozen=True)
class OTELRuleEventSpan:
    """Normalized OTEL span payload derived from a Rule event."""

    name: str
    trace_id: str
    parent_span_id: str
    attributes: dict[str, AttributeValue]
    start_time_unix_nano: int
    end_time_unix_nano: int
    error_message: str | None = None


@dataclass(frozen=True)
class OTELSinkConfig:
    """Configuration for OTEL sink creation."""

    enabled: bool
    endpoint: str | None
    headers: dict[str, str]
    service_name: str


@dataclass(frozen=True)
class OTELSDKModules:
    """References to the OTEL SDK classes/functions used by the sink."""

    tracer_provider_cls: type[Any]
    resource_cls: type[Any]
    batch_span_processor_cls: type[Any]
    otlp_span_exporter_cls: type[Any] | None
    span_context_cls: type[Any]
    non_recording_span_cls: type[Any]
    trace_flags_cls: type[Any]
    trace_state_cls: type[Any]
    status_cls: type[Any]
    status_code_cls: type[Any]
    span_kind: Any
    set_span_in_context: Any
    get_tracer_provider: Any


def _to_unix_nano(timestamp: datetime, /) -> int:
    return int(timestamp.astimezone(UTC).timestamp() * 1_000_000_000)


def _normalize_attribute_value(value: object) -> AttributeValue:
    """Coerce arbitrary metadata into a value OTEL span attributes accept."""
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        if all(isinstance(item, bool) for item in value):
            return [item for item in value]
        if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            return [item for item in value]
        if all(isinstance(item, float) for item in value):
            return [item for item in value]
        if all(isinstance(item, str) for item in value):
            return [item for item in value]
    if isinstance(value, tuple):
        return _normalize_attribute_value(list(value))
    return json.dumps(value, default=str, sort_keys=True)


def rule_event_to_otel_span(event: RuleCheckEvent) -> OTELRuleEventSpan:
    """Convert a Rule-check event into OTEL span data."""
    end_time_unix_nano = _to_unix_nano(event.timestamp)
    if event.execution_duration_ms is not None:
        start_time_unix_nano = max(
            0,
            end_time_unix_nano - int(event.execution_duration_ms * 1_000_000),
        )
    else:
        start_time_unix_nano = end_time_unix_nano

    attributes: dict[str, AttributeValue] = {
        "norma.check_id": event.check_id,
        "norma.agent_name": event.agent_name,
        "norma.rule_id": event.rule_id,
        "norma.rule_name": event.rule_name,
        "norma.check_stage": event.check_stage,
        "norma.applies_to": event.applies_to,
        "norma.action": event.action,
        "norma.matched": event.matched,
        "norma.confidence": event.confidence,
        "norma.event_timestamp": event.timestamp.isoformat(),
    }

    if event.execution_duration_ms is not None:
        attributes["norma.execution_duration_ms"] = event.execution_duration_ms
    if event.evaluator_name is not None:
        attributes["norma.evaluator_name"] = event.evaluator_name
    if event.selector_path is not None:
        attributes["norma.selector_path"] = event.selector_path
    if event.error_message is not None:
        attributes["norma.error_message"] = event.error_message

    for key, value in sorted(event.metadata.items()):
        if key in _DEBUG_METADATA_ATTRIBUTE_KEYS:
            continue
        attributes[f"norma.metadata.{key}"] = _normalize_attribute_value(value)

    return OTELRuleEventSpan(
        name="norma.rule_check",
        trace_id=event.trace_id,
        parent_span_id=event.span_id,
        attributes=attributes,
        start_time_unix_nano=start_time_unix_nano,
        end_time_unix_nano=end_time_unix_nano,
        error_message=event.error_message,
    )


class _NoOpRuleEventSink(BaseRuleEventSink):
    """Inactive sink that reports events it cannot export as dropped."""

    def is_active(self) -> bool:
        return False

    def write_events(self, events: Sequence[RuleCheckEvent]) -> SinkResult:
        return SinkResult(accepted=0, dropped=len(events))


class OTELRuleEventSink(BaseRuleEventSink):
    """Emit Rule-check events as OpenTelemetry spans."""

    def __init__(
        self,
        *,
        tracer_provider: Any,
        tracer: Any,
        sdk_modules: OTELSDKModules,
        provider_owned: bool,
    ) -> None:
        self._tracer_provider: Any = tracer_provider
        self._tracer: Any = tracer
        self._sdk_modules = sdk_modules
        self._provider_owned = provider_owned

    def write_events(self, events: Sequence[RuleCheckEvent]) -> SinkResult:
        accepted = 0
        dropped = 0

        for event in events:
            try:
                self._write_event(event)
                accepted += 1
            except Exception:
                logger.warning("Failed to emit Rule event to OTEL", exc_info=True)
                dropped += 1

        return SinkResult(accepted=accepted, dropped=dropped)

    def flush(self) -> None:
        force_flush = getattr(self._tracer_provider, "force_flush", None)
        if callable(force_flush) and force_flush() is False:
            raise RuntimeError("Norma OTEL export did not finish; check exporter connectivity.")

    def close(self) -> None:
        if not self._provider_owned:
            return
        shutdown = getattr(self._tracer_provider, "shutdown", None)
        if callable(shutdown):
            shutdown()

    def _write_event(self, event: RuleCheckEvent) -> None:
        span_data = rule_event_to_otel_span(event)
        parent_context = self._build_parent_context(span_data)
        span = self._tracer.start_span(
            span_data.name,
            context=parent_context,
            kind=self._sdk_modules.span_kind.INTERNAL,
            start_time=span_data.start_time_unix_nano,
        )
        span.set_attributes(span_data.attributes)
        if span_data.error_message:
            record_exception = getattr(span, "record_exception", None)
            if callable(record_exception):
                record_exception(RuntimeError(span_data.error_message))
            set_status = getattr(span, "set_status", None)
            if callable(set_status):
                set_status(
                    self._sdk_modules.status_cls(
                        self._sdk_modules.status_code_cls.ERROR,
                        span_data.error_message,
                    )
                )
        span.end(end_time=span_data.end_time_unix_nano)

    def _build_parent_context(self, span_data: OTELRuleEventSpan) -> object | None:
        trace_id = span_data.trace_id
        parent_span_id = span_data.parent_span_id

        if not validate_trace_id(trace_id) or trace_id == "0" * 32:
            return None
        if not validate_span_id(parent_span_id) or parent_span_id == "0" * 16:
            parent_span_id = _generate_span_id()

        parent_span_context = self._sdk_modules.span_context_cls(
            trace_id=int(trace_id, 16),
            span_id=int(parent_span_id, 16),
            is_remote=True,
            trace_flags=self._sdk_modules.trace_flags_cls(
                self._sdk_modules.trace_flags_cls.SAMPLED
            ),
            trace_state=self._sdk_modules.trace_state_cls(),
        )
        return cast(
            object,
            self._sdk_modules.set_span_in_context(
                self._sdk_modules.non_recording_span_cls(parent_span_context)
            ),
        )


def _resolve_otel_sink_config(config: JSONObject) -> OTELSinkConfig:
    """Resolve OTEL sink config from settings with per-sink overrides."""
    settings = get_settings()

    enabled = bool(config.get("enabled", settings.otel_enabled))

    endpoint_value = config.get("endpoint", settings.otel_endpoint)
    endpoint = str(endpoint_value) if endpoint_value else None

    headers_value = config.get("headers", settings.otel_headers)
    headers: dict[str, str] = {}
    if isinstance(headers_value, dict):
        headers = {str(key): str(value) for key, value in headers_value.items()}

    service_name_value = config.get("service_name", settings.otel_service_name)
    service_name = str(service_name_value) if service_name_value else "norma-sdk"

    return OTELSinkConfig(
        enabled=enabled,
        endpoint=endpoint,
        headers=headers,
        service_name=service_name,
    )


def _has_explicit_otel_exporter_configuration(config: OTELSinkConfig) -> bool:
    """Return whether an OTLP exporter should be configured for the sink."""
    if config.endpoint:
        return True
    return bool(
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    )


def _load_otel_sdk_modules() -> OTELSDKModules:
    """Import core OTEL SDK modules on demand so the sink remains optional."""
    from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-not-found]
    from opentelemetry.trace import (  # type: ignore[import-not-found]
        NonRecordingSpan,
        SpanContext,
        SpanKind,
        Status,
        StatusCode,
        TraceFlags,
        TraceState,
        get_tracer_provider,
        set_span_in_context,
    )

    return OTELSDKModules(
        tracer_provider_cls=TracerProvider,
        resource_cls=Resource,
        batch_span_processor_cls=BatchSpanProcessor,
        otlp_span_exporter_cls=None,
        span_context_cls=SpanContext,
        non_recording_span_cls=NonRecordingSpan,
        trace_flags_cls=TraceFlags,
        trace_state_cls=TraceState,
        status_cls=Status,
        status_code_cls=StatusCode,
        span_kind=SpanKind,
        set_span_in_context=set_span_in_context,
        get_tracer_provider=get_tracer_provider,
    )


def _load_otlp_span_exporter_cls() -> type[Any]:
    """Import the OTLP exporter only for a Norma-owned pipeline."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
        OTLPSpanExporter,
    )

    return cast(type[Any], OTLPSpanExporter)


def create_otel_rule_event_sink(
    config: JSONObject,
    *,
    tracer_provider: TracerProvider | None = None,
) -> BaseRuleEventSink:
    """Create the built-in OTEL Rule-event sink.

    An explicitly supplied or globally registered SDK provider is reused as an
    application-owned provider. Only providers created here receive a Norma
    exporter and processor and are shut down with the sink.
    """
    resolved_config = _resolve_otel_sink_config(config)
    if not resolved_config.enabled:
        return _NoOpRuleEventSink()

    try:
        sdk_modules = _load_otel_sdk_modules()
    except ImportError:
        logger.warning(_OTEL_NOOP_WARNING)
        return _NoOpRuleEventSink()

    external_provider: Any = tracer_provider
    if external_provider is None:
        global_provider = sdk_modules.get_tracer_provider()
        if isinstance(global_provider, sdk_modules.tracer_provider_cls):
            external_provider = global_provider

    if external_provider is not None:
        tracer = external_provider.get_tracer(_OTEL_INSTRUMENTATION_SCOPE)
        return OTELRuleEventSink(
            tracer_provider=external_provider,
            tracer=tracer,
            sdk_modules=sdk_modules,
            provider_owned=False,
        )

    exporter_cls = sdk_modules.otlp_span_exporter_cls
    if exporter_cls is None:
        try:
            exporter_cls = _load_otlp_span_exporter_cls()
        except ImportError:
            logger.warning(_OTEL_NOOP_WARNING)
            return _NoOpRuleEventSink()

    resource = sdk_modules.resource_cls.create({"service.name": resolved_config.service_name})
    tracer_provider = sdk_modules.tracer_provider_cls(resource=resource)

    if not _has_explicit_otel_exporter_configuration(resolved_config):
        logger.warning(_OTEL_EXPORTER_MISSING_WARNING)
        shutdown = getattr(tracer_provider, "shutdown", None)
        if callable(shutdown):
            shutdown()
        return _NoOpRuleEventSink()

    exporter_kwargs: dict[str, object] = {}
    if resolved_config.endpoint:
        exporter_kwargs["endpoint"] = resolved_config.endpoint
    if resolved_config.headers:
        exporter_kwargs["headers"] = resolved_config.headers
    exporter = exporter_cls(**exporter_kwargs)
    tracer_provider.add_span_processor(sdk_modules.batch_span_processor_cls(exporter))

    tracer = tracer_provider.get_tracer(_OTEL_INSTRUMENTATION_SCOPE)
    return OTELRuleEventSink(
        tracer_provider=tracer_provider,
        tracer=tracer,
        sdk_modules=sdk_modules,
        provider_owned=True,
    )
