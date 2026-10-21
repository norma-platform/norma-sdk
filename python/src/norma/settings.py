# Modified for Norma.
"""
Centralized configuration for Norma SDK.

This module provides typed configuration via pydantic-settings, supporting:
- Canonical NORMA_ environment variables
- Type coercion and validation
- Programmatic overrides

Usage:
    from norma.settings import settings

    # Access settings
    if settings.observability_enabled:
        send_events()

    # Settings are read from environment variables:
    # NORMA_RUNTIME_URL, NORMA_OBSERVABILITY_ENABLED, etc.
"""

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from norma._telemetry import DEFAULT_RULE_EVENT_SINK_NAME
from norma.models import JSONObject


class SDKSettings(BaseSettings):
    """
    Configuration for Norma SDK.

    Includes settings for:
    - Hosted connection (runtime_url, agent_key)
    - Observability/event batching
    - Logging verbosity

    All settings can be configured via environment variables with the
    NORMA_ prefix. For example:
        NORMA_RUNTIME_URL=https://your-tenant.runtime.example
        NORMA_AGENT_KEY=your-scoped-agent-key
        NORMA_OBSERVABILITY_ENABLED=true

    Settings can also be overridden programmatically via configure_settings().
    """

    model_config = SettingsConfigDict(
        env_prefix="NORMA_",
        populate_by_name=True,
        extra="ignore",
    )

    # Server connection
    runtime_url: str = Field(
        default="",
        validation_alias="NORMA_RUNTIME_URL",
        description="Base URL of the Norma server",
    )
    agent_key: str = Field(
        default="",
        validation_alias="NORMA_AGENT_KEY",
        description="API key for server authentication",
    )

    # Observability (event batching)
    observability_enabled: bool = Field(
        default=True,
        description="Enable sending events to server",
    )
    observability_sink_name: str = Field(
        default=DEFAULT_RULE_EVENT_SINK_NAME,
        description="Rule-event sink selection (default or registered/custom sink name)",
    )
    observability_sink_config: JSONObject = Field(
        default_factory=dict,
        description="JSON config payload passed to the selected rule-event sink",
    )
    otel_enabled: bool = Field(
        default=False,
        description="Enable the built-in OpenTelemetry rule-event sink",
    )
    otel_endpoint: str | None = Field(
        default=None,
        description="OTLP HTTP endpoint for OTEL rule-event export",
    )
    otel_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Headers to pass to the OTLP exporter",
    )
    otel_service_name: str = Field(
        default="norma-sdk",
        description="service.name resource for OTEL rule-event spans",
    )
    batch_size: int = Field(
        default=100,
        ge=1,
        description="Maximum events per batch",
    )
    flush_interval: float = Field(
        default=5.0,
        gt=0,
        description="Seconds between automatic flushes",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        description="Maximum retry attempts for failed batch sends",
    )
    retry_delay: float = Field(
        default=1.0,
        gt=0,
        description="Base delay between retries (seconds)",
    )
    shutdown_join_timeout: float = Field(
        default=5.0,
        gt=0,
        description="Seconds to wait for worker thread shutdown",
    )
    shutdown_flush_timeout: float = Field(
        default=5.0,
        gt=0,
        description="Seconds to wait for fallback shutdown flush",
    )
    shutdown_max_failed_flushes: int = Field(
        default=1,
        ge=1,
        description="Maximum consecutive failed flushes before giving up",
    )

    # Logging configuration
    log_enabled: bool = Field(
        default=True,
        description="Master switch for observability logging",
    )
    log_span_start: bool = Field(
        default=True,
        description="Log span start events",
    )
    log_span_end: bool = Field(
        default=True,
        description="Log span end events",
    )
    log_rule_eval: bool = Field(
        default=True,
        description="Log per-rule check events",
    )
    log_span_results: bool = Field(
        default=True,
        description="Log span results",
    )


# Global settings instance - loaded from environment at import time
settings = SDKSettings()


def load_settings_from_env() -> SDKSettings:
    """Load a fresh settings snapshot directly from environment variables."""
    return SDKSettings()


def get_settings() -> SDKSettings:
    """Get the current settings instance."""
    return settings


def configure_settings(**kwargs: Any) -> SDKSettings:
    """
    Override settings programmatically.

    This creates a new settings instance with the provided overrides
    and replaces the global instance.

    Args:
        **kwargs: Setting names and values to override

    Returns:
        The updated settings instance

    Example:
        configure_settings(
            observability_enabled=True,
            batch_size=50,
            log_rule_eval=False,
        )
    """
    global settings
    # Get current values as dict, apply overrides
    current = settings.model_dump()
    current.update(kwargs)
    settings = SDKSettings(**current)
    return settings
