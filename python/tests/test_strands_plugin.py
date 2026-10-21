# Modified for Norma.
"""Unit tests for Strands hook integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from norma.models import Decision


@pytest.fixture
def mock_strands_modules():
    """Mock the strands modules to avoid import errors."""
    with patch.dict(
        "sys.modules",
        {
            "strands": MagicMock(),
            "strands.hooks": MagicMock(),
            "strands.plugins": MagicMock(),
        },
    ):
        yield


@pytest.fixture
def mock_event_classes():
    """Create mock event classes."""
    BeforeInvocationEvent = type("BeforeInvocationEvent", (), {})  # noqa: N806
    BeforeModelCallEvent = type("BeforeModelCallEvent", (), {})  # noqa: N806
    AfterModelCallEvent = type("AfterModelCallEvent", (), {})  # noqa: N806
    BeforeToolCallEvent = type("BeforeToolCallEvent", (), {})  # noqa: N806
    AfterToolCallEvent = type("AfterToolCallEvent", (), {})  # noqa: N806
    BeforeNodeCallEvent = type("BeforeNodeCallEvent", (), {})  # noqa: N806
    AfterNodeCallEvent = type("AfterNodeCallEvent", (), {})  # noqa: N806
    Plugin = type("Plugin", (), {"__init__": lambda self: None})  # noqa: N806

    return {
        "BeforeInvocationEvent": BeforeInvocationEvent,
        "BeforeModelCallEvent": BeforeModelCallEvent,
        "AfterModelCallEvent": AfterModelCallEvent,
        "BeforeToolCallEvent": BeforeToolCallEvent,
        "AfterToolCallEvent": AfterToolCallEvent,
        "BeforeNodeCallEvent": BeforeNodeCallEvent,
        "AfterNodeCallEvent": AfterNodeCallEvent,
        "Plugin": Plugin,
    }


@pytest.fixture
def norma_hook(mock_strands_modules, mock_event_classes):
    """Create an NormaPlugin instance with mocked dependencies."""
    import importlib
    import sys

    import norma.integrations.strands.plugin as plugin_module

    hooks_mod = sys.modules["strands.hooks"]
    for name, cls in mock_event_classes.items():
        setattr(hooks_mod, name, cls)

    plugins_mod = sys.modules["strands.plugins"]
    plugins_mod.Plugin = mock_event_classes["Plugin"]

    importlib.reload(plugin_module)

    return plugin_module.NormaPlugin(agent_name="test-agent")


def test_action_error_with_deny_match():
    """Test _action_error returns deny error for deny action."""
    from norma.integrations.strands.plugin import _action_error

    # Create a mock match with deny action
    mock_match = MagicMock()
    mock_match.status = "matched"
    mock_match.action = "deny"
    mock_match.effective_action = None
    mock_match.rule_name = "test_rule"
    mock_match.result.message = "Access denied"

    # Create a mock result
    result = MagicMock(spec=Decision)
    result.checks = [mock_match]
    result.reason = None

    action_type, error = _action_error(result)

    assert action_type == "deny"
    assert "Rule violation [test_rule]" in str(error)
    assert "Access denied" in str(error)


def test_action_error_with_steer_match():
    """Test _action_error returns steer error for steer action."""
    from norma.integrations.strands.plugin import _action_error

    # Create a mock match with steer action
    mock_match = MagicMock()
    mock_match.status = "matched"
    mock_match.action = "steer"
    mock_match.effective_action = None
    mock_match.rule_name = "test_rule"
    mock_match.result.message = "Steering required"
    mock_match.steering_context.message = "Context message"

    # Create a mock result
    result = MagicMock(spec=Decision)
    result.checks = [mock_match]
    result.reason = None

    action_type, error = _action_error(result)

    assert action_type == "steer"
    assert "Steering required [test_rule]" in str(error)


def test_action_error_with_no_blocking_match():
    """Test _action_error returns None when no blocking matches."""
    from norma.integrations.strands.plugin import _action_error

    # Create a mock match with observe action
    mock_match = MagicMock()
    mock_match.status = "matched"
    mock_match.action = "observe"
    mock_match.effective_action = None

    # Create a mock result
    result = MagicMock(spec=Decision)
    result.checks = [mock_match]

    assert _action_error(result) is None


def test_action_error_with_empty_matches():
    """Test _action_error returns None with empty matches."""
    from norma.integrations.strands.plugin import _action_error

    result = MagicMock(spec=Decision)
    result.checks = []

    assert _action_error(result) is None


def test_action_error_with_default_checks():
    """A decision without checks has no blocking action."""
    from norma.integrations.strands.plugin import _action_error

    result = MagicMock(spec=Decision)
    result.checks = []

    assert _action_error(result) is None


def test_action_error_deny_takes_precedence():
    """Test deny takes precedence over steer regardless of order."""
    from norma import RuleViolationError
    from norma.integrations.strands.plugin import _action_error

    deny = MagicMock()
    deny.status = "matched"
    deny.action = "deny"
    deny.effective_action = None
    deny.rule_name = "deny-rule"
    deny.rule_id = 1
    deny.result.message = "Denied"

    steer = MagicMock()
    steer.status = "matched"
    steer.action = "steer"
    steer.effective_action = None
    steer.rule_name = "steer-rule"
    steer.rule_id = 2
    steer.result.message = "Steer"

    result = MagicMock(spec=Decision)
    # Steer appears first to ensure deny-first behavior
    result.checks = [steer, deny]
    result.reason = None

    action, err = _action_error(result)

    assert action == "deny"
    assert isinstance(err, RuleViolationError)
    assert err.rule_name == "deny-rule"


@pytest.mark.asyncio
async def test_evaluate_and_enforce_safe_result(norma_hook):
    """Test _evaluate_and_enforce with safe result."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        # Mock a safe result
        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = True
        mock_result.checks = []
        mock_evaluate.return_value = mock_result

        # Should not raise
        await norma_hook._evaluate_and_enforce(
            step_name="test_step", input="test input", step_type="llm", stage="pre"
        )


@pytest.mark.asyncio
async def test_evaluate_and_enforce_with_errors(norma_hook):
    """Test _evaluate_and_enforce fails closed on check errors."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        mock_error = MagicMock()
        mock_error.rule_name = "error-rule"

        mock_result = MagicMock(spec=Decision)
        mock_result.checks = [mock_error]
        mock_error.status = "error"
        mock_result.allowed = True
        mock_evaluate.return_value = mock_result

        with pytest.raises(RuntimeError):
            await norma_hook._evaluate_and_enforce(
                step_name="test_step", input="test input", step_type="llm", stage="pre"
            )


@pytest.mark.asyncio
async def test_evaluate_and_enforce_with_deny(norma_hook):
    """Test _evaluate_and_enforce raises error on deny action."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        from norma import RuleViolationError

        # Mock a deny result
        mock_match = MagicMock()
        mock_match.status = "matched"
        mock_match.action = "deny"
        mock_match.effective_action = None
        mock_match.rule_name = "test_rule"
        mock_match.result.message = "Denied"

        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = True
        mock_result.checks = [mock_match]
        mock_evaluate.return_value = mock_result

        with pytest.raises(RuleViolationError):
            await norma_hook._evaluate_and_enforce(
                step_name="test_step", input="test input", step_type="llm", stage="pre"
            )


@pytest.mark.asyncio
async def test_evaluate_and_enforce_with_steer(norma_hook):
    """Test _evaluate_and_enforce raises error on steer action."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        from norma import RuleSteerError

        # Mock a steer result
        mock_match = MagicMock()
        mock_match.status = "matched"
        mock_match.action = "steer"
        mock_match.effective_action = None
        mock_match.rule_name = "test_rule"
        mock_match.rule_id = 42
        mock_match.result.message = "Steer required"
        mock_match.result.metadata = {"risk": "high"}
        mock_match.steering_context.message = "Context"

        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = True
        mock_result.checks = [mock_match]
        mock_evaluate.return_value = mock_result

        with pytest.raises(RuleSteerError) as exc_info:
            await norma_hook._evaluate_and_enforce(
                step_name="test_step", input="test input", step_type="llm", stage="pre"
            )

        err = exc_info.value
        assert err.rule_name == "test_rule"
        assert err.rule_id == 42
        assert err.metadata == {"risk": "high"}
        assert "Context" in err.steering_context


@pytest.mark.asyncio
async def test_evaluate_and_enforce_unsafe_result(norma_hook):
    """Test _evaluate_and_enforce raises error on unsafe result."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        from norma import RuleViolationError

        # Mock an unsafe result
        mock_match = MagicMock()
        mock_match.status = "matched"
        mock_match.action = "observe"
        mock_match.effective_action = None
        mock_match.rule_name = "test_rule"
        mock_match.result.message = "Unsafe"

        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = False
        mock_result.checks = [mock_match]
        mock_result.reason = "Test reason"
        mock_evaluate.return_value = mock_result

        with pytest.raises(RuleViolationError) as exc_info:
            await norma_hook._evaluate_and_enforce(
                step_name="test_step", input="test input", step_type="llm", stage="pre"
            )

        assert "test_rule" in str(exc_info.value)


def test_extract_content_text_with_string(norma_hook):
    """Test _extract_content_text with string input."""
    result = norma_hook._extract_content_text("test string")
    assert result == "test string"


def test_extract_content_text_with_dict_text(norma_hook):
    """Test _extract_content_text with dict containing text."""
    result = norma_hook._extract_content_text({"text": "test text"})
    assert result == "test text"


def test_extract_content_text_with_dict_json(norma_hook):
    """Test _extract_content_text with dict containing json."""
    result = norma_hook._extract_content_text({"json": {"key": "value"}})
    assert "key" in result
    assert "value" in result


def test_extract_content_text_with_list(norma_hook):
    """Test _extract_content_text with list of content blocks."""
    content = [
        {"text": "First text"},
        {"text": "Second text"},
    ]
    result = norma_hook._extract_content_text(content)
    assert "First text" in result
    assert "Second text" in result


def test_extract_content_text_with_tool_use(norma_hook):
    """Test _extract_content_text with tool use block."""
    content = [
        {"toolUse": {"name": "test_tool"}},
    ]
    result = norma_hook._extract_content_text(content)
    assert "[tool_use: test_tool]" in result


def test_extract_content_text_with_empty_content(norma_hook):
    """Test _extract_content_text with empty content."""
    assert norma_hook._extract_content_text("") == ""
    assert norma_hook._extract_content_text(None) == ""
    assert norma_hook._extract_content_text([]) == ""


def test_extract_tool_data_before_tool(norma_hook):
    """Test _extract_tool_data with BeforeToolCallEvent."""
    from norma.integrations.strands.plugin import BeforeToolCallEvent

    # Create a mock with the right spec so isinstance works
    event = MagicMock(spec=BeforeToolCallEvent)
    event.selected_tool = MagicMock()
    event.selected_tool.tool_name = "test_tool"
    event.tool_use = {"name": "test_tool", "input": {"param": "value"}}

    tool_name, tool_data = norma_hook._extract_tool_data(event)

    assert tool_name == "test_tool"
    assert tool_data == {"param": "value"}


def test_extract_tool_data_after_tool_success(norma_hook):
    """Test _extract_tool_data with AfterToolCallEvent (success)."""
    from norma.integrations.strands.plugin import AfterToolCallEvent

    # Create a mock with the right spec so isinstance works
    event = MagicMock(spec=AfterToolCallEvent)
    event.selected_tool = MagicMock()
    event.selected_tool.tool_name = "test_tool"
    event.tool_use = {"name": "test_tool"}
    event.exception = None
    event.result = {"content": [{"text": "result text"}]}

    tool_name, tool_data = norma_hook._extract_tool_data(event)

    assert tool_name == "test_tool"
    assert "result text" in tool_data


def test_extract_tool_data_after_tool_error(norma_hook):
    """Test _extract_tool_data with AfterToolCallEvent (error)."""
    from norma.integrations.strands.plugin import AfterToolCallEvent

    # Create a mock with the right spec so isinstance works
    event = MagicMock(spec=AfterToolCallEvent)
    event.selected_tool = MagicMock()
    event.selected_tool.tool_name = "test_tool"
    event.tool_use = {"name": "test_tool"}
    event.exception = Exception("Tool failed")
    event.result = {}

    tool_name, tool_data = norma_hook._extract_tool_data(event)

    assert tool_name == "test_tool"
    assert "ERROR" in tool_data
    assert "Tool failed" in tool_data


def test_hook_initialization():
    """Test NormaPlugin initialization."""
    from norma.integrations.strands.plugin import NormaPlugin

    hook = NormaPlugin(
        agent_name="test-agent",
        event_types=None,
        on_violation_callback=None,
        enable_logging=False,
    )

    assert hook.agent_name == "test-agent"
    assert hook.event_types is None
    assert hook.on_violation_callback is None
    assert hook.enable_logging is False


def test_init_agent_captures_complete_strands_tool_registry(norma_hook):
    # Given: a Strands agent exposing its complete normalized registry
    registry = MagicMock()
    registry.get_all_tool_specs.return_value = [
        {
            "name": "search_docs",
            "description": "Search documentation",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    ]
    agent = MagicMock(tool_registry=registry)

    # When: the plugin is initialized against that agent
    norma_hook.init_agent(agent)

    # Then: the full registry is normalized without executable objects
    assert norma_hook._available_tools() == [
        {
            "name": "search_docs",
            "description": "Search documentation",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]


def test_available_tools_fails_closed_when_strands_registry_raises(norma_hook, caplog):
    # Given: a Strands registry that cannot provide a complete tool set
    registry = MagicMock()
    registry.get_all_tool_specs.side_effect = RuntimeError("registry unavailable")
    norma_hook._tool_registry = registry

    # When: tool definitions are requested
    tools = norma_hook._available_tools()

    # Then: tools remain absent and the integration records the failure
    assert tools is None
    assert "Unable to capture complete Strands tool definitions" in caplog.text


def test_hook_with_callback():
    """Test NormaPlugin with violation callback."""
    from norma.integrations.strands.plugin import NormaPlugin

    callback = MagicMock()
    hook = NormaPlugin(agent_name="test-agent", on_violation_callback=callback)

    # Test callback invocation
    mock_result = MagicMock(spec=Decision)
    hook._invoke_callback("test_rule", "pre", mock_result)

    callback.assert_called_once()
    call_args = callback.call_args[0]
    assert call_args[0]["agent"] == "test-agent"
    assert call_args[0]["rule_name"] == "test_rule"
    assert call_args[0]["stage"] == "pre"


def test_raise_error_with_runtime_error(norma_hook):
    """Test _raise_error with use_runtime_error=True."""
    from norma import RuleViolationError

    error = RuleViolationError(message="Test error")

    with pytest.raises(RuntimeError) as exc_info:
        norma_hook._raise_error(error, use_runtime_error=True)

    assert "Test error" in str(exc_info.value)


def test_raise_error_without_runtime_error(norma_hook):
    """Test _raise_error with use_runtime_error=False."""
    from norma import RuleViolationError

    error = RuleViolationError(message="Test error")

    with pytest.raises(RuleViolationError):
        norma_hook._raise_error(error, use_runtime_error=False)


def test_extract_user_message_from_list(norma_hook):
    """Test _extract_user_message_from_list."""
    messages = [
        {"role": "system", "content": "System message"},
        {"role": "user", "content": "User message"},
        {"role": "assistant", "content": "Assistant message"},
    ]

    result = norma_hook._extract_user_message_from_list(messages)
    assert result == "User message"


def test_extract_context(norma_hook):
    """Test _extract_context from invocation_state."""

    class DummyEvent:
        invocation_state = {"context": {"request_id": "abc123"}}

    assert norma_hook._extract_context(DummyEvent()) == {"request_id": "abc123"}


def test_extract_context_missing(norma_hook):
    """Test _extract_context returns None when missing."""

    class DummyEvent:
        invocation_state = {}

    assert norma_hook._extract_context(DummyEvent()) is None


def test_extract_user_message_from_list_reverse(norma_hook):
    """Test _extract_user_message_from_list with reverse=True."""
    messages = [
        {"role": "user", "content": "First user message"},
        {"role": "assistant", "content": "Assistant message"},
        {"role": "user", "content": "Last user message"},
    ]

    result = norma_hook._extract_user_message_from_list(messages, reverse=True)
    assert result == "Last user message"


def test_extract_user_message_from_list_empty(norma_hook):
    """Test _extract_user_message_from_list with empty list."""
    assert norma_hook._extract_user_message_from_list([]) == ""
    assert norma_hook._extract_user_message_from_list(None) == ""


def test_extract_user_message_no_user_role(norma_hook):
    """Test _extract_user_message_from_list when no user role messages exist."""
    messages = [
        {"role": "system", "content": "System message"},
        {"role": "assistant", "content": "Assistant message"},
    ]
    assert norma_hook._extract_user_message_from_list(messages) == ""


@pytest.mark.asyncio
async def test_evaluate_and_enforce_unsafe_no_reason_with_match(norma_hook):
    """Test _evaluate_and_enforce with unsafe result, no reason, but has match."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        from norma import RuleViolationError

        # Mock an unsafe result with no reason but with a match that has a message
        mock_match = MagicMock()
        mock_match.status = "matched"
        mock_match.action = "observe"
        mock_match.effective_action = None
        mock_match.rule_name = "test_rule"
        mock_match.result.message = "Match message"

        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = False
        mock_result.checks = [mock_match]
        mock_result.reason = None  # No reason
        mock_evaluate.return_value = mock_result

        with pytest.raises(RuleViolationError) as exc_info:
            await norma_hook._evaluate_and_enforce(
                step_name="test_step", input="test input", step_type="llm", stage="pre"
            )

        assert "test_rule" in str(exc_info.value)
        assert "Match message" in str(exc_info.value)


def test_register_hooks_with_custom_event_list(mock_strands_modules, mock_event_classes):
    """Test init_agent with custom event_types."""
    with patch(
        "norma.integrations.strands.plugin.BeforeInvocationEvent",
        mock_event_classes["BeforeInvocationEvent"],
    ):  # noqa: E501
        with patch(
            "norma.integrations.strands.plugin.BeforeModelCallEvent",
            mock_event_classes["BeforeModelCallEvent"],
        ):  # noqa: E501
            with patch(
                "norma.integrations.strands.plugin.BeforeToolCallEvent",
                mock_event_classes["BeforeToolCallEvent"],
            ):  # noqa: E501
                with patch(
                    "norma.integrations.strands.plugin.AfterToolCallEvent",
                    mock_event_classes["AfterToolCallEvent"],
                ):  # noqa: E501
                    with patch(
                        "norma.integrations.strands.plugin.AfterModelCallEvent",
                        mock_event_classes["AfterModelCallEvent"],
                    ):  # noqa: E501
                        with patch(
                            "norma.integrations.strands.plugin.BeforeNodeCallEvent",
                            mock_event_classes["BeforeNodeCallEvent"],
                        ):  # noqa: E501
                            with patch(
                                "norma.integrations.strands.plugin.AfterNodeCallEvent",
                                mock_event_classes["AfterNodeCallEvent"],
                            ):  # noqa: E501
                                with patch(
                                    "norma.integrations.strands.plugin.Plugin",
                                    mock_event_classes["Plugin"],
                                ):  # noqa: E501
                                    from norma.integrations.strands.plugin import (
                                        AfterModelCallEvent,
                                        BeforeModelCallEvent,
                                        NormaPlugin,
                                    )

                                    # Create hook with custom event list
                                    hook = NormaPlugin(
                                        agent_name="test-agent",
                                        event_types=[
                                            BeforeModelCallEvent,
                                            AfterModelCallEvent,
                                        ],
                                        enable_logging=True,
                                    )

                                    # Create mock agent with add_hook method
                                    mock_agent = MagicMock()
                                    mock_agent.add_hook = MagicMock()

                                    # Register hooks via init_agent
                                    hook.init_agent(mock_agent)

                                    # Verify only specified events were registered
                                    assert mock_agent.add_hook.call_count == 2


@pytest.mark.asyncio
async def test_check_before_invocation(norma_hook):
    """Test check_before_invocation hook."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        from norma.integrations.strands.plugin import BeforeInvocationEvent

        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = True
        mock_result.checks = []
        mock_evaluate.return_value = mock_result

        event = MagicMock(spec=BeforeInvocationEvent)
        event.messages = [{"role": "user", "content": "test message"}]

        await norma_hook.check_before_invocation(event)

        mock_evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_check_before_model(norma_hook):
    """Test check_before_model hook."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        from norma.integrations.strands.plugin import BeforeModelCallEvent

        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = True
        mock_result.checks = []
        mock_evaluate.return_value = mock_result

        event = MagicMock(spec=BeforeModelCallEvent)
        event.invocation_state = {"messages": [{"role": "user", "content": "test"}]}

        await norma_hook.check_before_model(event)

        mock_evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_check_after_model(norma_hook):
    """Test check_after_model hook."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        from norma.integrations.strands.plugin import AfterModelCallEvent

        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = True
        mock_result.checks = []
        mock_evaluate.return_value = mock_result

        event = MagicMock(spec=AfterModelCallEvent)
        event.stop_response = MagicMock()
        event.stop_response.message = {"content": [{"text": "model output"}]}
        event.invocation_state = {
            "messages": [{"role": "user", "content": "test input"}],
            "context": {"request_id": "ctx-1"},
        }

        await norma_hook.check_after_model(event)

        mock_evaluate.assert_called_once()
        assert mock_evaluate.call_args.kwargs["input"] == "test input"
        assert mock_evaluate.call_args.kwargs["context"] == {"request_id": "ctx-1"}


@pytest.mark.asyncio
async def test_check_before_tool(norma_hook):
    """Test check_before_tool hook."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        from norma.integrations.strands.plugin import BeforeToolCallEvent

        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = True
        mock_result.checks = []
        mock_evaluate.return_value = mock_result

        event = MagicMock(spec=BeforeToolCallEvent)
        event.selected_tool = MagicMock()
        event.selected_tool.tool_name = "test_tool"
        event.tool_use = {"name": "test_tool", "input": {"param": "value"}}

        await norma_hook.check_before_tool(event)

        mock_evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_check_after_tool(norma_hook):
    """Test check_after_tool hook."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        from norma.integrations.strands.plugin import AfterToolCallEvent

        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = True
        mock_result.checks = []
        mock_evaluate.return_value = mock_result

        event = MagicMock(spec=AfterToolCallEvent)
        event.selected_tool = MagicMock()
        event.selected_tool.tool_name = "test_tool"
        event.tool_use = {"name": "test_tool"}
        event.exception = None
        event.result = {"content": [{"text": "result"}]}

        await norma_hook.check_after_tool(event)

        mock_evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_check_before_node(norma_hook):
    """Test check_before_node hook."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        from norma.integrations.strands.plugin import BeforeNodeCallEvent

        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = True
        mock_result.checks = []
        mock_evaluate.return_value = mock_result

        event = MagicMock(spec=BeforeNodeCallEvent)
        event.node_id = "test_node"
        event.invocation_state = {"messages": [{"role": "user", "content": "test"}]}

        await norma_hook.check_before_node(event)

        mock_evaluate.assert_called_once()


@pytest.mark.asyncio
async def test_check_after_node(norma_hook):
    """Test check_after_node hook."""
    with patch("norma.integrations.strands.plugin.norma.evaluate_rules") as mock_evaluate:  # noqa: E501
        from norma.integrations.strands.plugin import AfterNodeCallEvent

        mock_result = MagicMock(spec=Decision)
        mock_result.allowed = True
        mock_result.checks = []
        mock_evaluate.return_value = mock_result

        event = MagicMock(spec=AfterNodeCallEvent)
        event.node_id = "test_node"
        event.invocation_state = {"output": "test output", "input": "node input"}

        await norma_hook.check_after_node(event)

        mock_evaluate.assert_called_once()
        assert mock_evaluate.call_args.kwargs["input"] == "node input"


def test_extract_tool_data_no_selected_tool(norma_hook):
    """Test _extract_tool_data when selected_tool is None."""
    from norma.integrations.strands.plugin import BeforeToolCallEvent

    event = MagicMock(spec=BeforeToolCallEvent)
    event.selected_tool = None
    event.tool_use = {"name": "fallback_tool", "input": {"key": "value"}}

    tool_name, tool_data = norma_hook._extract_tool_data(event)

    assert tool_name == "fallback_tool"
    assert tool_data == {"key": "value"}


def test_extract_content_text_with_citations(norma_hook):
    """Test _extract_content_text with citations content."""
    content = [
        {
            "citationsContent": {
                "content": [{"text": "citation text 1"}, {"text": "citation text 2"}]
            }
        }
    ]

    result = norma_hook._extract_content_text(content)
    assert "citation text 1" in result
    assert "citation text 2" in result


def test_extract_content_text_with_tool_result(norma_hook):
    """Test _extract_content_text with tool result block."""
    content = [{"toolResult": {"content": [{"text": "tool result text"}]}}]

    result = norma_hook._extract_content_text(content)
    assert "tool result text" in result


def test_extract_content_text_with_non_dict_block(norma_hook):
    """Test _extract_content_text with non-dict blocks."""
    content = ["string block", {"text": "dict block"}]

    result = norma_hook._extract_content_text(content)
    assert "string block" in result
    assert "dict block" in result


def test_extract_content_text_with_unknown_type(norma_hook):
    """Test _extract_content_text with unknown content type."""
    content = 12345  # Some other type

    result = norma_hook._extract_content_text(content)
    assert result == "12345"


def test_extract_messages_before_model_with_input(norma_hook):
    """Test _extract_messages with BeforeModelCallEvent using input field."""
    from norma.integrations.strands.plugin import BeforeModelCallEvent

    event = MagicMock(spec=BeforeModelCallEvent)
    event.invocation_state = {"input": "direct input text"}

    input_text, output_text = norma_hook._extract_messages(event)

    assert input_text == "direct input text"
    assert output_text == ""


def test_extract_messages_after_model_no_response(norma_hook):
    """Test _extract_messages with AfterModelCallEvent when stop_response is None."""
    from norma.integrations.strands.plugin import AfterModelCallEvent

    event = MagicMock(spec=AfterModelCallEvent)
    event.stop_response = None
    event.invocation_state = {}

    input_text, output_text = norma_hook._extract_messages(event)

    assert input_text == ""
    assert output_text == ""


def test_extract_messages_after_model_missing_invocation_state(norma_hook):
    """Test _extract_messages handles missing invocation_state."""
    from norma.integrations.strands.plugin import AfterModelCallEvent

    event = MagicMock(spec=AfterModelCallEvent)
    event.stop_response = None
    event.invocation_state = None

    input_text, output_text = norma_hook._extract_messages(event)

    assert input_text == ""
    assert output_text == ""


def test_extract_messages_after_model_with_response(norma_hook):
    """Test _extract_messages with AfterModelCallEvent when stop_response exists."""
    from norma.integrations.strands.plugin import AfterModelCallEvent

    event = MagicMock(spec=AfterModelCallEvent)
    event.stop_response = MagicMock()
    event.stop_response.message = {"content": [{"text": "output text"}]}
    event.invocation_state = {"messages": [{"role": "user", "content": "user input"}]}

    input_text, output_text = norma_hook._extract_messages(event)

    assert input_text == "user input"
    assert output_text == "output text"


def test_extract_messages_after_model_with_input_field(norma_hook):
    """Test _extract_messages with AfterModelCallEvent using input field."""
    from norma.integrations.strands.plugin import AfterModelCallEvent

    event = MagicMock(spec=AfterModelCallEvent)
    event.stop_response = MagicMock()
    event.stop_response.message = {"content": [{"text": "output text"}]}
    event.invocation_state = {"input": "input text"}

    input_text, output_text = norma_hook._extract_messages(event)

    assert input_text == "input text"
    assert output_text == "output text"


def test_extract_messages_before_node_with_input(norma_hook):
    """Test _extract_messages with BeforeNodeCallEvent using input field."""
    from norma.integrations.strands.plugin import BeforeNodeCallEvent

    event = MagicMock(spec=BeforeNodeCallEvent)
    event.invocation_state = {"input": "node input"}

    input_text, output_text = norma_hook._extract_messages(event)

    assert input_text == "node input"
    assert output_text == ""


def test_extract_messages_after_node_with_result(norma_hook):
    """Test _extract_messages with AfterNodeCallEvent using result field."""
    from norma.integrations.strands.plugin import AfterNodeCallEvent

    event = MagicMock(spec=AfterNodeCallEvent)
    event.invocation_state = {"result": "node result", "input": "node input"}

    input_text, output_text = norma_hook._extract_messages(event)

    assert input_text == "node input"
    assert output_text == "node result"


def test_extract_messages_after_node_with_response(norma_hook):
    """Test _extract_messages with AfterNodeCallEvent using response field."""
    from norma.integrations.strands.plugin import AfterNodeCallEvent

    event = MagicMock(spec=AfterNodeCallEvent)
    event.invocation_state = {"response": "node response"}

    input_text, output_text = norma_hook._extract_messages(event)

    assert input_text == ""
    assert output_text == "node response"


def test_extract_messages_after_node_with_messages(norma_hook):
    """Test _extract_messages with AfterNodeCallEvent using messages field."""
    from norma.integrations.strands.plugin import AfterNodeCallEvent

    event = MagicMock(spec=AfterNodeCallEvent)
    # When messages is in the state, it's treated as content to extract
    event.invocation_state = {
        "messages": [
            {"role": "user", "content": "user msg"},
            {"role": "assistant", "content": "assistant msg"},
        ]
    }

    input_text, output_text = norma_hook._extract_messages(event)

    assert input_text == "user msg"
    assert output_text == "user msg\nassistant msg"
