# Modified for Norma.
"""Public hosted CLI diagnostics never expose configuration values."""

import httpx
import pytest

from norma import cli


def test_configuration_is_offline_and_redacted(monkeypatch, capsys):
    # Given: valid secrets and a transport that must never open.
    monkeypatch.setenv("NORMA_RUNTIME_URL", "https://private.example.com/customer")
    monkeypatch.setenv("NORMA_AGENT_KEY", "private-agent-secret")
    monkeypatch.setattr(cli, "NormaClient", lambda **kwargs: pytest.fail("network access"))
    # When: checking configuration locally.
    assert cli.main(["config", "check"]) == 0
    # Then: only validation statuses appear.
    output = capsys.readouterr().out
    assert "NORMA_RUNTIME_URL: valid" in output
    assert "private" not in output


def test_invalid_configuration_does_not_echo_input(monkeypatch, capsys):
    # Given: credentials embedded in an invalid endpoint.
    monkeypatch.setenv("NORMA_RUNTIME_URL", "https://sensitive:password@example.com")
    monkeypatch.delenv("NORMA_AGENT_KEY", raising=False)
    # When: validation fails.
    assert cli.main(["config", "check"]) == 1
    # Then: secrets are absent from the diagnostic.
    assert "sensitive" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_doctor_is_a_single_scoped_read(monkeypatch, capsys):
    # Given: an authorized in-memory endpoint.
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"agent": {"agent_name": "sample-agent"}, "steps": []})

    client_type = cli.NormaClient
    monkeypatch.setattr(
        cli,
        "NormaClient",
        lambda **kwargs: client_type(
            "https://tenant.example.com/api/runtime",
            agent_key="test-key",
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    # When: an explicit access diagnostic is requested.
    assert await cli._doctor("sample-agent") == 0
    # Then: no check, mutation or infrastructure action occurs.
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/runtime/api/v1/agents/sample-agent"
    assert "not verified" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 302, 500])
async def test_doctor_failure_is_redacted(monkeypatch, capsys, status):
    # Given: a remote failure whose body may contain confidential data.
    client_type = cli.NormaClient
    monkeypatch.setattr(
        cli,
        "NormaClient",
        lambda **kwargs: client_type(
            "https://tenant.example.com",
            agent_key="test-key",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status,
                    json={"detail": "confidential"},
                    headers={"Location": "https://other.example"},
                )
            ),
            **kwargs,
        ),
    )
    # When: the read fails.
    assert await cli._doctor("sample-agent") == 1
    # Then: only the status is reported.
    assert "confidential" not in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "malformed", "wrong-agent"])
async def test_doctor_does_not_confirm_unverified_access(monkeypatch, capsys, failure):
    # Given: a failed connection or an unusable successful HTTP response.
    def respond(request):
        if failure == "network":
            raise httpx.ConnectError("confidential endpoint", request=request)
        if failure == "malformed":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"agent": {"agent_name": "another-agent"}, "steps": []})

    client_type = cli.NormaClient
    monkeypatch.setattr(
        cli,
        "NormaClient",
        lambda **kwargs: client_type(
            "https://tenant.example.com",
            agent_key="test-key",
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    # When: an explicit diagnostic fails.
    assert await cli._doctor("sample-agent") == 1
    # Then: no access confirmation or sensitive exception text is printed.
    output = capsys.readouterr().out
    assert "Agent access verified" not in output
    assert "confidential" not in output
