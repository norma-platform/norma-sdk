# Modified for Norma.
"""Install and exercise the built wheel outside any source checkout."""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


def main() -> None:
    package = Path(__file__).resolve().parents[1]
    expected_version = tomllib.loads((package / "pyproject.toml").read_text())["project"]["version"]
    expected_name = f"norma_sdk-{expected_version}-py3-none-any.whl"
    wheels = [wheel for wheel in (package / "dist").glob("norma_sdk-*.whl")
              if wheel.name == expected_name]
    if len(wheels) != 1:
        raise SystemExit(f"Expected the current SDK wheel {expected_name} in dist/")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        roots = {name.split("/")[0] for name in names}
        expected = {"norma", wheels[0].name.removesuffix("-py3-none-any.whl") + ".dist-info"}
        if roots != expected:
            raise SystemExit(f"Unexpected distribution namespaces: {roots}")
        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
        if metadata.get("Version") != expected_version:
            raise SystemExit("Built wheel metadata version does not match pyproject.toml")
        deps = metadata.get_all("Requires-Dist", [])
        base = {dependency.split(";")[0].split(">")[0].split("<")[0].split("=")[0]
                for dependency in deps if "extra ==" not in dependency}
        if base != {"httpx", "pydantic", "pydantic-settings", "docstring-parser"}:
            raise SystemExit(f"Unexpected runtime dependencies: {base}")
        if not any(name.endswith("/NOTICE") for name in names):
            raise SystemExit("NOTICE is missing from the distribution")
        rule_tree = ast.parse(archive.read("norma/models/rules.py"))
        rule_model = next(node for node in rule_tree.body if isinstance(node, ast.ClassDef)
                          and node.name == "RuleDefinition")
        rule_fields = {node.target.id for node in rule_model.body if isinstance(node, ast.AnnAssign)
                       and isinstance(node.target, ast.Name)}
        if {"template", "template_values", "parameters"} & rule_fields:
            raise SystemExit("Retired template or shared-parameter fields remain in the Python wheel")
        if "norma/models/parameter_binding.py" in names:
            raise SystemExit("Retired policy parameter binding module remains in the Python wheel")
        agents_tree = ast.parse(archive.read("norma/agents.py"))
        if any(arg.arg == "rendered_state" for node in ast.walk(agents_tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]):
            raise SystemExit("Retired rendered_state filter remains in the Python wheel")
        error_tree = ast.parse(archive.read("norma/models/errors.py"))
        if any(isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in {
                "RULE_TEMPLATE_CONFLICT", "TEMPLATE_PARAMETER_INVALID", "TEMPLATE_RENDER_ERROR"
            } for target in node.targets
        ) for node in ast.walk(error_tree)):
            raise SystemExit("Retired template errors remain in the Python wheel")
    with tempfile.TemporaryDirectory(prefix="norma-wheel-") as directory:
        root = Path(directory)
        wheel = root / wheels[0].name
        shutil.copy2(wheels[0], wheel)
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["PYTHONNOUSERSITE"] = "1"
        subprocess.run([sys.executable, "-m", "venv", str(root / "env")], check=True)
        python = root / "env/bin/python"
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "--quiet", str(wheel)], cwd=root, env=environment, check=True)
        smoke = '''
import asyncio, importlib.util, inspect, json, pathlib, sys
import httpx
import norma
from norma import NormaClient, Step, checks, operations, check_finish
from norma.models import Decision
from norma.models import RuleDefinition
from norma.models.errors import ErrorCode
from norma.models.server import RuleRecord
from norma import validate_authored_controls
assert pathlib.Path(norma.__file__).is_relative_to(pathlib.Path(sys.prefix))
assert importlib.util.find_spec("agents") is None
for retired_field in ("template", "template_values", "parameters"):
    assert retired_field not in RuleDefinition.model_fields
assert importlib.util.find_spec("norma.models.parameter_binding") is None
assert set(__import__("typing").get_args(RuleRecord.model_fields["status"].annotation)) == {"hosted", "unsupported"}
assert "rendered_state" not in inspect.signature(norma.agents.list_agent_rules_typed).parameters
assert "rendered_state" not in inspect.signature(norma.agents.list_agent_rules).parameters
for retired in ("RULE_TEMPLATE_CONFLICT", "TEMPLATE_PARAMETER_INVALID", "TEMPLATE_RENDER_ERROR"):
    assert retired not in ErrorCode.__members__
ordinary_rule = RuleDefinition.model_validate({
    "execution": "server", "condition": {"evaluator": {"name": "json", "config": {}}},
    "action": {"decision": "deny"},
})
assert ordinary_rule.execution == "server"
for retired_field in ("template", "template_values", "parameters"):
    try:
        RuleDefinition.model_validate({**ordinary_rule.model_dump(), retired_field: {}})
    except Exception:
        pass
    else:
        raise AssertionError(f"Installed RuleDefinition accepted retired field {retired_field}")
try:
    import norma.integrations.openai_agents
except RuntimeError as exc:
    assert "norma-sdk[openai-agents]" in str(exc)
else:
    raise AssertionError("Missing Agents dependency did not produce installation guidance")
assert importlib.util.find_spec("google") is None
import norma.integrations.google_adk
try:
    norma.integrations.google_adk.NormaPlugin
except RuntimeError as exc:
    assert "google-adk" in str(exc)
else:
    raise AssertionError("Missing optional dependency did not produce installation guidance")
assert not hasattr(norma, "rule")
assert callable(norma.evaluate_rules)
assert hasattr(norma.models, "RuleMatch")
control_capabilities = {
    "contract": "rule-controls-v1", "fingerprint": "c" * 64,
    "manifests": [{
        "evaluator": "json", "evaluator_version": "3.2.1",
        "schema_fingerprint": "a" * 64,
        "slots": [{
            "evaluator": "json", "evaluator_version": "3.2.1",
            "schema_fingerprint": "a" * 64,
            "path_template": "/config/field_constraints/*/min",
            "control_types": ["number"], "predicates": [], "applicability": [],
            "affects": "decision",
            "expected_value_schema": {"types": ["number"], "minimum": 0,
                "maximum": 365, "integer": True},
            "requires_server_validation": True,
        }],
    }],
}
authored_control_rule = json.dumps({
    "execution": "server",
    "controls": {"age": {"type": "number", "label": "Age", "default": 90,
        "minimum": 0, "maximum": 365, "integer": True}},
    "condition": {"selector": {"path": "output"}, "evaluator": {
        "name": "json", "config": {
            "field_constraints": {"age": {"min": {"$param": "age"}}}}}},
    "action": {"decision": "deny"},
})
control_validation = validate_authored_controls(authored_control_rule, control_capabilities)
assert control_validation.status == "requires_server_validation"
assert control_validation.requires_server_validation
assert len(control_validation.references) == 1
assert control_validation.references[0].key == "age"
from norma.models.errors import ErrorCode, make_public_error_code
from norma.errors import NormaAPIError
import importlib
rule_codes = (
    "RULE_NOT_FOUND", "RULE_VERSION_NOT_FOUND", "RULE_BINDING_NOT_FOUND",
    "RULE_NAME_CONFLICT", "RULE_BINDING_CONFLICT", "RULE_IN_USE",
    "POLICY_RULE_INCOMPATIBLE", "RULE_BINDING_INCOMPATIBLE",
)
for name in rule_codes:
    assert ErrorCode[name].value == name
    response = httpx.Response(409, json={"error_code": name},
                              request=httpx.Request("GET", "https://runtime.example.test"))
    assert NormaAPIError(response).code == make_public_error_code(ErrorCode[name])
for module_name in ("norma", "norma.checks", "norma.operations", "norma.agents",
                    "norma.checkpoints", "norma.finish", "norma.errors"):
    module = importlib.import_module(module_name)
    for name in ("state", "decision_from_wire", "normalize_answers", "rule_definition_from_wire",
                 "feedback_from_wire", "decode_error_code", "request_check",
                 "request_remote_check"):
        assert not hasattr(module, name), (module_name, name)

effects = []
@norma.checkpoint()
def cold_action():
    effects.append("ran")
try:
    cold_action()
except norma.CheckError:
    pass
else:
    raise AssertionError("Uninitialized checkpoint did not block")
assert effects == []
seen = []
def endpoint(request):
    seen.append(request)
    return httpx.Response(200, json={"allowed": True, "confidence": 1,
                                    "execution_contract": "hosted-v1"})
async def main():
    async with NormaClient("https://runtime.example.test/api/runtime", agent_key="test-key",
                           transport=httpx.MockTransport(endpoint)) as client:
        decision = await checks.check(client, "external-wheel-agent",
                                      Step(type="tool", name="read", input={}), "pre")
        assert isinstance(decision, Decision)
        assert decision.allowed
        assert set(decision.model_dump()) == {
            "allowed", "reason", "checks", "execution_contract", "recovery_feedback"
        }
    assert len(seen) == 1
    assert seen[0].url.path == "/api/runtime/api/v1/checks"
    assert json.loads(seen[0].content)["execution_mode"] == "hosted"

    # Exercise Console controls through the installed public API. A receipt is
    # deliberately nonempty so read adapters cannot pass by dropping fields.
    receipt = {
        "contract": "rule-controls-v1", "capabilities_fingerprint": "a" * 64,
        "declaration_fingerprint": "b" * 64, "defaults": {"age": 90},
        "effective_values": {"age": 120}, "values_fingerprint": "c" * 64,
        "value_sources": {"age": "agent_override"},
        "references": [{"key": "age", "source_pointer": "/controls/age/default",
            "destination_pointer": "/condition/evaluator/config/field_constraints/age/min",
            "evaluator_pointer": "/condition/evaluator", "slot": {"control_types": ["number"]}}],
        "overridden_keys": ["age"], "agent_generation": "generation-1",
        "agent_name": "external-wheel-agent", "rule_id": 7, "rule_version": 3,
        "override_revision": 4, "effective_fingerprint": "d" * 64,
    }
    definition = {"execution": "server", "condition": {"evaluator": {"name": "json",
        "config": {"field_constraints": {"age": {"min": 120}}}}},
        "action": {"decision": "deny"}}
    headers = {"X-Norma-Control-Contract": "rule-controls-v1"}
    request_log = []
    operation = {
        "id": "operation-controls-1", "operation_key": "key-controls-1",
        "agent_name": "external-wheel-agent", "agent_generation": "generation-1",
        "control_contract": "rule-controls-v1", "created_at": "2026-10-26T00:00:00Z",
        "request_fingerprint": "request-fingerprint",
        "request": {"agent_name": "external-wheel-agent", "operation_key": "key-controls-1",
            "step": {"type": "tool", "name": "read", "input": {}}, "stage": "pre",
            "target_type": None, "target_id": None, "error_policy": "block",
            "unresolved_policy": "block", "evidence": []},
        "decision": {"outcome": "allow", "reason": "checked",
            "check": {"allowed": True, "confidence": 1, "execution_contract": "hosted-v1",
                "control_contract": "rule-controls-v1", "agent_generation": "generation-1",
                "control_resolutions": [receipt]}},
        "rules": [], "observations": [], "verifications": [], "discrepancy": "none",
        "current_outcome": "not_observed",
    }
    finish = {"execution_contract": "hosted-v1", "control_contract": "rule-controls-v1",
        "agent_generation": "generation-1", "control_resolutions": [receipt],
        "verdict": "pass", "reason": "checked", "reason_codes": [], "held_by": [],
        "would_hold_by": [], "continuation": {"count": 0, "max": 2},
        "allow_finish": True, "outcome": "completed", "provider_input_tokens": None}
    mode = {"bad_ack": None}
    def controls_endpoint(req):
        request_log.append(req)
        assert req.headers.get("X-Norma-Control-Contract") == "rule-controls-v1"
        path = req.url.path
        if path.endswith("/agents/register"):
            body = {"created": True, "overwrite_applied": False,
                "rules": [{"id": 7, "name": "age-check", "rule": definition}],
                "control_contract": "rule-controls-v1", "agent_generation": "generation-1",
                "control_resolutions": [receipt]}
        elif path.endswith("/agents/external-wheel-agent/rules"):
            body = {"rules": [{"id": 7, "name": "age-check", "rule": definition}],
                "control_contract": "rule-controls-v1", "agent_generation": "generation-1",
                "control_resolutions": [receipt]}
        elif (
            path.endswith("/checks")
            and json.loads(req.content).get("step", {}).get("type") == "turn"
        ):
            body = finish
        elif path.endswith("/checks"):
            body = {"allowed": True, "confidence": 1, "execution_contract": "hosted-v1",
                "control_contract": "rule-controls-v1", "agent_generation": "generation-1",
                "control_resolutions": [receipt]}
        elif path.endswith("/operations/operation-controls-1"):
            body = operation
        else:
            raise AssertionError(f"Unexpected installed SDK route: {path}")
        response_headers = {"content-type": "application/json", **headers}
        if mode["bad_ack"] == "header": response_headers.pop("X-Norma-Control-Contract")
        if mode["bad_ack"] == "envelope": body = {**body, "control_contract": "other"}
        return httpx.Response(200, json=body, headers=response_headers)

    async with NormaClient("https://runtime.example.test/api/runtime", agent_key="test-key",
                           control_contract="rule-controls-v1",
                           transport=httpx.MockTransport(controls_endpoint)) as client:
        registered = await norma.agents.register_agent(client, norma.models.Agent(
            agent_name="external-wheel-agent"))
        listed = await norma.agents.list_agent_rules_typed(client, "external-wheel-agent")
        checked = await checks.check(client, "external-wheel-agent",
            Step(type="tool", name="read", input={}), "pre")
        operation_read = await operations.get(client, "operation-controls-1",
            agent_name="external-wheel-agent")
        turn = norma.models.TurnRecord(schema_version="1", platform="probe", session_id="s",
            turn_id="t", final_message="done", evidence_source="sdk",
            continuation={"count": 0, "max": 2})
        finished = await check_finish(turn, client=client, agent_name="external-wheel-agent")
        assert registered["control_resolutions"][0].effective_values == {"age": 120}
        assert listed.control_resolutions[0].override_revision == 4
        assert checked.control_resolutions[0].value_sources == {"age": "agent_override"}
        assert operation_read.decision.control_resolutions[0].defaults == {"age": 90}
        assert finished.control_resolutions[0].effective_values == {"age": 120}
        assert len(request_log) == 5
        assert all(req.headers["X-Norma-Control-Contract"] == "rule-controls-v1"
                   for req in request_log)

    # Negotiation failures are terminal and never retry a check action.
    for bad_ack in ("header", "envelope"):
        mode["bad_ack"] = bad_ack
        failed_requests = []
        def bad_endpoint(req):
            failed_requests.append(req)
            response = controls_endpoint(req)
            return response
        async with NormaClient("https://runtime.example.test/api/runtime", agent_key="test-key",
                               control_contract="rule-controls-v1",
                               transport=httpx.MockTransport(bad_endpoint)) as client:
            try:
                await checks.check(client, "external-wheel-agent",
                    Step(type="tool", name="read", input={}), "pre")
            except norma.UnsupportedControlContractError:
                pass
            else:
                raise AssertionError("Missing control acknowledgement was accepted")
        assert len(failed_requests) == 1
        assert json.loads(failed_requests[0].content)["stage"] == "pre"
    assert all(req.url.path != "/api/runtime/api/v1/operations/observe-metrics"
               for req in request_log)
asyncio.run(main())
print(json.dumps({"installed_file": norma.__file__, "version": norma.__version__,
                  "hosted_contract": "passed", "control_artifact_probe": "passed",
                  "runtime_dependencies_only": True}))
'''
        subprocess.run([str(python), "-c", smoke], cwd=root, env=environment, check=True)
    print(json.dumps({"wheel": wheels[0].name, "outside_repository_install": "passed"}))


if __name__ == "__main__":
    main()
