# Modified for Norma.
"""Agent management operations for Norma SDK."""

from typing import Any, Literal, cast

from norma.models import Agent
from norma.models.server import AgentRulesResponse

from .client import NormaClient
from .errors import raise_for_norma_status
from .validation import ensure_agent_name


def _agent_rules_query_params(
    *,
    enabled_state: Literal["enabled", "disabled", "all"] | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
) -> dict[str, str] | None:
    """Build optional query params for the Rules endpoint."""
    params: dict[str, str] = {}
    if enabled_state is not None:
        params["enabled_state"] = enabled_state
    if target_type is not None:
        params["target_type"] = target_type
    if target_id is not None:
        params["target_id"] = target_id
    return params or None


async def _register_agent(
    client: NormaClient,
    agent: Agent,
    steps: list[dict[str, Any]] | None = None,
    conflict_mode: Literal["strict", "overwrite"] = "strict",
    *,
    target_type: str | None = None,
    target_id: str | None = None,
) -> dict[str, Any]:
    """Register an agent with the server via /register endpoint.

    When ``target_type`` and ``target_id`` are both supplied, the server
    merges Rules bound to that target into the returned set. The two
    fields must be supplied together.

    With a hosted Agent Key, register the identity in Console first, then use
    ``conflict_mode="strict"`` for that exact allowed name. Scoped keys cannot
    create identities or use overwrite mode. Strict mode is the safe default;
    authorized management clients must request overwrite explicitly.

    Raises:
        httpx.HTTPStatusError: The server rejected registration. A 403 includes
            Norma setup guidance and preserves the original request and response.
    """

    if (target_type is None) != (target_id is None):
        raise ValueError("target_type and target_id must be supplied together.")

    agent_dict = agent.to_dict()
    agent_dict["agent_name"] = ensure_agent_name(str(agent_dict.get("agent_name", "")))
    payload: dict[str, Any] = {
        "agent": agent_dict,
        "steps": steps or [],
        "conflict_mode": conflict_mode,
    }
    if target_type is not None and target_id is not None:
        payload["target_type"] = target_type
        payload["target_id"] = target_id

    headers = None
    response = await client.http_client.post(
        "/api/v1/agents/register",
        json=payload,
        headers=headers,
    )
    raise_for_norma_status(response)
    return cast(dict[str, Any], response.json())


async def get_agent(client: NormaClient, agent_name: str) -> dict[str, Any]:
    """Get agent details by name from the server."""
    normalized_name = ensure_agent_name(agent_name)
    response = await client.http_client.get(f"/api/v1/agents/{normalized_name}")
    response.raise_for_status()
    return cast(dict[str, Any], response.json())


async def list_agents(
    client: NormaClient,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List all registered agents from the server."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = ensure_agent_name(cursor)
    response = await client.http_client.get("/api/v1/agents", params=params)
    response.raise_for_status()
    data = cast(dict[str, Any], response.json())
    return data


async def _list_agent_rules(
    client: NormaClient,
    agent_name: str,
    *,
    enabled_state: Literal["enabled", "disabled", "all"] | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
) -> dict[str, Any]:
    """List effective Rules for an agent.

    When state filters are omitted, the server returns all effective Rules,
    including enabled and disabled Rules. Callers can narrow that view by
    passing enabled_state.

    When ``target_type`` and ``target_id`` are both supplied, the server
    merges Rules bound to that target into the returned set. The two
    fields must be supplied together.
    """
    if (target_type is None) != (target_id is None):
        raise ValueError("target_type and target_id must be supplied together.")
    normalized_name = ensure_agent_name(agent_name)
    params = _agent_rules_query_params(
        enabled_state=enabled_state,
        target_type=target_type,
        target_id=target_id,
    )
    if params is None:
        response = await client.http_client.get(f"/api/v1/agents/{normalized_name}/rules")
    else:
        response = await client.http_client.get(
            f"/api/v1/agents/{normalized_name}/rules",
            params=params,
        )
    response.raise_for_status()
    return cast(dict[str, Any], response.json())


async def list_agent_rules_typed(
    client: NormaClient,
    agent_name: str,
    *,
    enabled_state: Literal["enabled", "disabled", "all"] | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
) -> AgentRulesResponse:
    """List effective Rules for an agent with a typed response.

    When ``target_type`` and ``target_id`` are both supplied, the server merges
    Rules bound to that target into the returned set.
    """
    if (target_type is None) != (target_id is None):
        raise ValueError("target_type and target_id must be supplied together.")
    normalized_name = ensure_agent_name(agent_name)
    params = _agent_rules_query_params(
        enabled_state=enabled_state,
        target_type=target_type,
        target_id=target_id,
    )
    if params is None:
        response = await client.http_client.get(f"/api/v1/agents/{normalized_name}/rules")
    else:
        response = await client.http_client.get(
            f"/api/v1/agents/{normalized_name}/rules",
            params=params,
        )
    response.raise_for_status()
    return _public_rules(response.json())


def _public_rules(data: dict[str, Any]) -> AgentRulesResponse:
    rules = []
    for item in data["rules"]:
        definition = item["rule"]
        if definition.get("execution") == "sdk":
            status = "unsupported"
        elif definition.get("condition") is None or definition.get("action") is None:
            status = "unsupported"
        else:
            status = "hosted"
        rules.append(
            {
                "id": item["id"],
                "name": item["name"],
                "status": status,
                "definition": definition if status == "hosted" else None,
            }
        )
    controls = {
        name: data[name]
        for name in ("control_contract", "agent_generation", "control_resolutions")
        if name in data
    }
    return AgentRulesResponse.model_validate({"rules": rules, **controls})


async def list_agent_rules(client: NormaClient, agent_name: str) -> AgentRulesResponse:
    """Read effective Rules; historical local definitions are marked unsupported."""
    return _public_rules(await _list_agent_rules(client, agent_name))


async def register_agent(
    client: NormaClient,
    agent: Agent,
    steps: list[dict[str, Any]] | None = None,
    conflict_mode: Literal["strict", "overwrite"] = "strict",
    *,
    target_type: str | None = None,
    target_id: str | None = None,
) -> dict[str, Any]:
    """Validate an existing agent's checkpoints and return its hosted Rules."""
    data = await _register_agent(
        client, agent, steps, conflict_mode, target_type=target_type, target_id=target_id
    )
    result = dict(data)
    projection = _public_rules(data)
    result["rules"] = [rule.model_dump(mode="json") for rule in projection.rules]
    for name in ("control_contract", "agent_generation", "control_resolutions"):
        value = getattr(projection, name)
        if value is not None:
            result[name] = value
    return result
