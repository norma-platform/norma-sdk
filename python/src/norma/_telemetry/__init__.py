# Modified for Norma.
"""Shared telemetry contracts for Norma."""

from .sink_selection import (
    DEFAULT_RULE_EVENT_SINK_NAME,
    REGISTERED_RULE_EVENT_SINK_NAME,
    RuleEventSinkFactory,
    RuleEventSinkFactoryRegistry,
    RuleEventSinkSelection,
    SinkSelectionError,
)
from .sinks import (
    AsyncRuleEventSink,
    BaseAsyncRuleEventSink,
    BaseRuleEventSink,
    RuleEventSink,
    SinkResult,
)
from .trace_context import (
    TraceContext,
    TraceContextProvider,
    clear_trace_context_provider,
    get_trace_context_from_provider,
    set_trace_context_provider,
)

__all__ = [
    "AsyncRuleEventSink",
    "BaseAsyncRuleEventSink",
    "BaseRuleEventSink",
    "RuleEventSink",
    "SinkResult",
    "DEFAULT_RULE_EVENT_SINK_NAME",
    "REGISTERED_RULE_EVENT_SINK_NAME",
    "RuleEventSinkFactory",
    "RuleEventSinkFactoryRegistry",
    "RuleEventSinkSelection",
    "SinkSelectionError",
    "TraceContext",
    "TraceContextProvider",
    "clear_trace_context_provider",
    "get_trace_context_from_provider",
    "set_trace_context_provider",
]
