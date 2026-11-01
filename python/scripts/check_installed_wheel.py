# Modified for Norma.
"""Install and exercise the built wheel outside any source checkout."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


def main() -> None:
    package = Path(__file__).resolve().parents[1]
    expected_version = tomllib.loads((package / "pyproject.toml").read_text())["project"]["version"]
    expected_name = f"norma_sdk-{expected_version}-py3-none-any.whl"
    wheels = [
        wheel for wheel in (package / "dist").glob("norma_sdk-*.whl") if wheel.name == expected_name
    ]
    if len(wheels) != 1:
        raise SystemExit(f"Expected the current SDK wheel {expected_name} in dist/")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        roots = {name.split("/")[0] for name in names}
        expected = {
            "norma",
            "_norma_config",
            wheels[0].name.removesuffix("-py3-none-any.whl") + ".dist-info",
        }
        if roots != expected:
            raise SystemExit(f"Unexpected distribution namespaces: {roots}")
        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
        if metadata.get("Version") != expected_version:
            raise SystemExit("Built wheel metadata version does not match pyproject.toml")
        deps = metadata.get_all("Requires-Dist", [])
        base = {
            dependency.split(";")[0].split(">")[0].split("<")[0].split("=")[0]
            for dependency in deps
            if "extra ==" not in dependency
        }
        if base != {
            "httpx",
            "pydantic",
            "pydantic-settings",
            "docstring-parser",
            "jsonschema",
            "referencing",
            "ruamel-yaml",
        }:
            raise SystemExit(f"Unexpected runtime dependencies: {base}")
        if not any(name.endswith("/NOTICE") for name in names):
            raise SystemExit("NOTICE is missing from the distribution")
        if "_norma_config/instrumentation.schema.json" not in names:
            raise SystemExit("Private offline validator schema is missing from the distribution")
        rule_tree = ast.parse(archive.read("norma/models/rules.py"))
        rule_model = next(
            node
            for node in rule_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "RuleDefinition"
        )
        rule_fields = {
            node.target.id
            for node in rule_model.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        if {"template", "template_values", "parameters"} & rule_fields:
            raise SystemExit(
                "Retired template or shared-parameter fields remain in the Python wheel"
            )
        if "norma/models/parameter_binding.py" in names:
            raise SystemExit("Retired policy parameter binding module remains in the Python wheel")
        agents_tree = ast.parse(archive.read("norma/agents.py"))
        if any(
            arg.arg == "rendered_state"
            for node in ast.walk(agents_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        ):
            raise SystemExit("Retired rendered_state filter remains in the Python wheel")
        error_tree = ast.parse(archive.read("norma/models/errors.py"))
        if any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id
                in {"RULE_TEMPLATE_CONFLICT", "TEMPLATE_PARAMETER_INVALID", "TEMPLATE_RENDER_ERROR"}
                for target in node.targets
            )
            for node in ast.walk(error_tree)
        ):
            raise SystemExit("Retired template errors remain in the Python wheel")
    with tempfile.TemporaryDirectory(prefix="norma-wheel-") as directory:
        root = Path(directory)
        wheel = root / wheels[0].name
        shutil.copy2(wheels[0], wheel)
        if (
            hashlib.sha256(wheel.read_bytes()).digest()
            != hashlib.sha256(wheels[0].read_bytes()).digest()
        ):
            raise SystemExit("Copied install wheel does not match the qualified artifact")
        streamed_example = root / "streamed_finish"
        shutil.copytree(package / "examples" / "streamed_finish", streamed_example)
        stream_agent_name = "installed-stream-smoke"
        shutil.copy2(
            streamed_example / "config" / "example-agent.json",
            streamed_example / "config" / f"{stream_agent_name}.json",
        )
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["NORMA_AGENT_NAME"] = stream_agent_name
        environment["NORMA_RUNTIME_URL"] = "https://runtime.example.test/api/runtime"
        environment["NORMA_AGENT_KEY"] = "installed-stream-smoke-key"
        subprocess.run([sys.executable, "-m", "venv", str(root / "env")], check=True)
        python = root / "env/bin/python"
        dependency_root = root / "runtime-dependencies"
        dependency_root.mkdir()
        source_site_packages = Path(sysconfig.get_paths()["purelib"])
        for dependency in source_site_packages.iterdir():
            if (
                dependency.name
                in {
                    "norma",
                    "_norma_config",
                    "agents",
                    "google",
                }
                or dependency.name.startswith("norma_sdk-")
                or dependency.name.endswith(".pth")
            ):
                continue
            (dependency_root / dependency.name).symlink_to(
                dependency,
                target_is_directory=dependency.is_dir(),
            )
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--no-deps",
                "--quiet",
                str(wheel),
            ],
            cwd=root,
            env=environment,
            check=True,
        )
        installed_package = root / "env/lib/python3.12/site-packages"
        with zipfile.ZipFile(wheel) as archive:
            for member in (
                "norma/__init__.py",
                "_norma_config/coverage.py",
                "_norma_config/cli.py",
            ):
                if (installed_package / member).read_bytes() != archive.read(member):
                    raise SystemExit(f"Installed wheel file differs from its artifact: {member}")
        runtime_environment = dict(environment)
        runtime_environment["PYTHONPATH"] = str(dependency_root)
        offline_dir = root / "offline-cli"
        offline_dir.mkdir()
        (offline_dir / ".env").write_text(
            "NORMA_OBSERVABILITY_ENABLED=poison-dotenv-value\n", encoding="utf-8"
        )
        config = offline_dir / "installed-validator-agent.json"
        config.write_text(
            '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}',
            encoding="utf-8",
        )
        isolated_environment = dict(runtime_environment)
        isolated_environment["NORMA_OBSERVABILITY_ENABLED"] = "poison-environment-value"
        result = subprocess.run(
            [str(root / "env/bin/norma"), "config", "validate", str(config), "--json"],
            cwd=offline_dir,
            env=isolated_environment,
            capture_output=True,
            text=True,
            check=True,
        )
        if json.loads(result.stdout)["valid"] is not True:
            raise SystemExit("Installed offline validator rejected its valid configuration")
        if "poison-" in result.stdout + result.stderr:
            raise SystemExit("Installed offline validator exposed an SDK setting value")
        (offline_dir / "sitecustomize.py").write_text(
            "import socket\n"
            "import sys\n"
            "def blocked(*args, **kwargs): raise AssertionError('network attempted')\n"
            "socket.create_connection = blocked\n"
            "socket.socket.connect = blocked\n"
            "class ImportGuard:\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if fullname == 'norma' or fullname.startswith('application_sentinel'):\n"
            "            raise AssertionError('application namespace imported')\n"
            "sys.meta_path.insert(0, ImportGuard())\n",
            encoding="utf-8",
        )
        coverage_config = offline_dir / "installed-coverage-agent.json"
        coverage_config.write_text(
            '{"version":2,"entrypoints":["application_sentinel.run"],'
            '"checkpoints":{"send":{"target":"application_sentinel.send",'
            '"input_from":"all_kwargs"}}}',
            encoding="utf-8",
        )
        local_rules = offline_dir / "installed-rules.json"
        local_rules.write_text(
            '{"rules":[{"condition":{"selector":{"path":"input.recipient"},'
            '"evaluator":{"name":"json","config":{}}},'
            '"scope":{"stages":["pre"]}}]}',
            encoding="utf-8",
        )
        isolated_environment = dict(runtime_environment)
        isolated_environment["PYTHONPATH"] = os.pathsep.join(
            (str(offline_dir), str(dependency_root))
        )
        coverage_result = subprocess.run(
            [
                str(root / "env/bin/norma"),
                "config",
                "check",
                str(coverage_config),
                "--rules",
                str(local_rules),
                "--allow-unknown",
                "--json",
            ],
            cwd=offline_dir,
            env=isolated_environment,
            capture_output=True,
            text=True,
            check=True,
        )
        coverage_report = json.loads(coverage_result.stdout)
        if (
            coverage_report["valid"] is not True
            or coverage_report["coverage_complete"] is not False
            or coverage_report["rules_checked"] != 1
            or coverage_report["coverage"][0]["status"] != "unknown"
        ):
            raise SystemExit("Installed offline Rule coverage check failed")
        contract_config = offline_dir / "installed-input-schema-agent.json"
        contract_config.write_text(
            '{"version":2,"entrypoints":["application_sentinel.run"],'
            '"checkpoints":{"send":{"target":"application_sentinel.send",'
            '"input_from":{"provider":"application_sentinel.payload"},'
            '"input_schema":{"type":"object","required":["recipient"],'
            '"properties":{"recipient":{"type":"string"}},'
            '"additionalProperties":false}}}}',
            encoding="utf-8",
        )
        contract_rules = offline_dir / "installed-input-schema-rules.json"
        contract_rules.write_text(
            '{"rules":[{"condition":{"selector":{"path":"input"},'
            '"evaluator":{"name":"json","config":{"field_types":'
            '{"recipient":"string"}}}},"scope":{"stages":["pre"]}}]}',
            encoding="utf-8",
        )
        contract_result = subprocess.run(
            [
                str(root / "env/bin/norma"),
                "config",
                "check",
                str(contract_config),
                "--rules",
                str(contract_rules),
                "--json",
            ],
            cwd=offline_dir,
            env=isolated_environment,
            capture_output=True,
            text=True,
            check=True,
        )
        contract_report = json.loads(contract_result.stdout)
        if (
            contract_report["valid"] is not True
            or contract_report["coverage_complete"] is not True
            or not contract_report["coverage"]
            or any(item["status"] != "covered" for item in contract_report["coverage"])
        ):
            raise SystemExit("Installed offline input-schema Rule proof failed")
        all_params_config = offline_dir / "installed-all-params-agent.json"
        all_params_config.write_text(
            '{"version":2,"entrypoints":["application_sentinel.run"],'
            '"checkpoints":{"send":{"target":"application_sentinel.send",'
            '"input_from":"all_params"}}}',
            encoding="utf-8",
        )
        all_params_result = subprocess.run(
            [
                str(root / "env/bin/norma"),
                "config",
                "check",
                str(all_params_config),
                "--rules",
                str(local_rules),
                "--allow-unknown",
                "--json",
            ],
            cwd=offline_dir,
            env=isolated_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        all_params_report = json.loads(all_params_result.stdout)
        if (
            all_params_result.returncode != 1
            or all_params_report["valid"] is not False
            or all_params_report["coverage"]
            or not any(
                item["code"] == "capability" and item["path"] == "/checkpoints/send/input_from"
                for item in all_params_report["diagnostics"]
            )
        ):
            raise SystemExit("Installed offline CLI failed to reject all_params capability")
        version_result = subprocess.run(
            [str(root / "env/bin/norma"), "version"],
            cwd=root,
            env=runtime_environment,
            capture_output=True,
            text=True,
            check=True,
        )
        if version_result.stdout.strip() != expected_version:
            raise SystemExit("Installed norma version command returned the wrong package version")
        legacy_environment = dict(runtime_environment)
        legacy_result = subprocess.run(
            [str(root / "env/bin/norma"), "config", "check"],
            cwd=root,
            env=legacy_environment,
            capture_output=True,
            text=True,
            check=True,
        )
        if "NORMA_RUNTIME_URL: valid" not in legacy_result.stdout:
            raise SystemExit("Installed legacy environment check was not preserved")
        smoke = """
import asyncio, importlib.util, inspect, json, os, pathlib, sys
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
assert set(__import__("typing").get_args(
    RuleRecord.model_fields["status"].annotation
)) == {"hosted", "unsupported"}
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
assert callable(norma.configure)
assert callable(norma.aconfigure)
assert callable(norma.parse_instrumentation)
declarative = norma.parse_instrumentation(
    '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}'
)
assert declarative["entrypoints"] == ["app.run"]
assert pathlib.Path(norma.__file__).with_name("instrumentation.schema.json").is_file()
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

    class AllKwargsApp:
        pass

    wheel_app = AllKwargsApp()
    all_kwargs_checks = []

    async def mutate_with_kwargs(positional, *, token, omitted="default-only"):
        token["value"] = "after-action"
        return "done"

    async def run_all_kwargs():
        return await wheel_app.mutate("positional-only", token={"value": "before"})

    wheel_app.mutate = mutate_with_kwargs
    wheel_app.run = run_all_kwargs
    try:
        await norma.aconfigure(
            {
                "version": 2,
                "entrypoints": ["wheel_app.run"],
                "checkpoints": {
                    "mutate": {"target": "wheel_app.missing", "input_from": "all_params"}
                },
            },
            agent_name="wheel-all-params",
            bindings={"wheel_app": wheel_app},
            connection={
                "runtime_url": "https://runtime.example.test/api/runtime",
                "agent_key": "wheel-all-params-key",
                "transport": httpx.MockTransport(lambda request: (_ for _ in ()).throw(
                    AssertionError("all_params rejection attempted network access")
                )),
                "observability_enabled": False,
                "refresh_interval_seconds": 0,
            },
        )
    except ValueError as exc:
        assert "all_params" in str(exc) and "input_from" in str(exc)
    else:
        raise AssertionError("Installed Python runtime accepted unsupported all_params")

    def all_kwargs_endpoint(request):
        path = request.url.path
        if path.endswith("/agents/wheel-all-kwargs"):
            return httpx.Response(200, json={"agent": {"agent_name": "wheel-all-kwargs"}})
        if path.endswith("/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            all_kwargs_checks.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"allowed": True, "confidence": 1, "execution_contract": "hosted-v1"},
            )
        raise AssertionError(f"Unexpected all_kwargs route: {path}")

    configured = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["wheel_app.run"],
            "checkpoints": {
                "mutate": {"target": "wheel_app.mutate", "input_from": "all_kwargs"}
            },
        },
        agent_name="wheel-all-kwargs",
        bindings={"wheel_app": wheel_app},
        connection={
            "runtime_url": "https://runtime.example.test/api/runtime",
            "agent_key": "wheel-all-kwargs-key",
            "transport": httpx.MockTransport(all_kwargs_endpoint),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await wheel_app.run() == "done"
        assert [body["stage"] for body in all_kwargs_checks] == ["pre", "post"]
        assert all(
            body["step"]["input"] == {"token": {"value": "before"}}
            for body in all_kwargs_checks
        )
    finally:
        await configured.aclose()

    class GuardedApp:
        pass

    guarded_app = GuardedApp()
    guard_effects = []
    guard_checks = []

    async def guarded_run():
        return await guarded_app.send()

    async def guarded_send():
        guard_effects.append("send")
        return "sent"

    def guarded_payload(_invocation):
        return {"private-marker": "schema-rejected"}

    guarded_app.run = guarded_run
    guarded_app.send = guarded_send
    guarded_app.payload = guarded_payload

    def guarded_endpoint(request):
        path = request.url.path
        if path.endswith("/agents/wheel-schema"):
            return httpx.Response(200, json={"agent": {"agent_name": "wheel-schema"}})
        if path.endswith("/agents/wheel-schema/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            guard_checks.append(request)
            return httpx.Response(
                200, json={"allowed": True, "confidence": 1, "execution_contract": "hosted-v1"}
            )
        raise AssertionError(f"Unexpected guarded-wheel route: {path}")

    schema_handle = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["guarded_app.run"],
            "checkpoints": {
                "send": {
                    "target": "guarded_app.send",
                    "input_from": {"provider": "guarded_app.payload"},
                    "input_schema": {
                        "type": "object",
                        "required": ["recipient"],
                        "properties": {"recipient": {"type": "string"}},
                        "additionalProperties": False,
                    },
                }
            },
        },
        agent_name="wheel-schema",
        bindings={"guarded_app": guarded_app},
        connection={
            "runtime_url": "https://runtime.example.test/api/runtime",
            "agent_key": "wheel-schema-key",
            "transport": httpx.MockTransport(guarded_endpoint),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )

    try:
        try:
            await guarded_app.run()
        except norma.CheckError as exc:
            safe_message = str(exc)
            assert "/checkpoints/send/input_schema" in safe_message
            assert "/recipient" in safe_message
            assert "private-marker" not in safe_message
        else:
            raise AssertionError("Installed input schema did not block a missing field")
        assert guard_effects == []
        assert guard_checks == []

        async def reassigned_send():
            guard_effects.append("reassigned-send")
            return "reassigned"

        guarded_app.send = reassigned_send
        try:
            await guarded_app.run()
        except norma.CheckError as exc:
            assert "guarded_app.send" in str(exc)
            assert "changed after" in str(exc)
        else:
            raise AssertionError("Installed slot ownership guard allowed a replaced target")
        assert guard_effects == []
        assert guard_checks == []
    finally:
        await schema_handle.aclose()

    provider_app = GuardedApp()
    provider_effects = []
    provider_checks = []

    async def provider_run():
        return await provider_app.send()

    async def provider_send():
        provider_effects.append("send")
        return "sent"

    def fail_provider(_invocation):
        raise RuntimeError("private-provider-exception")

    provider_app.run = provider_run
    provider_app.send = provider_send
    provider_app.payload = fail_provider

    def provider_endpoint(request):
        path = request.url.path
        if path.endswith("/agents/wheel-provider"):
            return httpx.Response(200, json={"agent": {"agent_name": "wheel-provider"}})
        if path.endswith("/agents/wheel-provider/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            provider_checks.append(request)
            return httpx.Response(
                200, json={"allowed": True, "confidence": 1, "execution_contract": "hosted-v1"}
            )
        raise AssertionError(f"Unexpected provider-wheel route: {path}")

    provider_handle = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["provider_app.run"],
            "checkpoints": {
                "send": {
                    "target": "provider_app.send",
                    "input_from": {"provider": "provider_app.payload"},
                }
            },
        },
        agent_name="wheel-provider",
        bindings={"provider_app": provider_app},
        connection={
            "runtime_url": "https://runtime.example.test/api/runtime",
            "agent_key": "wheel-provider-key",
            "transport": httpx.MockTransport(provider_endpoint),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        try:
            await provider_app.run()
        except norma.CheckError as exc:
            assert "/checkpoints/send/input_from" in str(exc)
            assert "private-provider-exception" not in str(exc)
        else:
            raise AssertionError("Installed provider failure was not blocked")
        assert provider_effects == []
        assert provider_checks == []
    finally:
        await provider_handle.aclose()

    context_app = GuardedApp()
    context_effects = []
    context_checks = []

    async def context_run_dummy(a, b):
        return await context_app.dummy_tool(a=a, b=b)

    async def context_dummy_tool(a, b):
        context_effects.append("dummy_tool")
        return f"{a}-{b}"

    context_app.run_dummy = context_run_dummy
    context_app.dummy_tool = context_dummy_tool

    def context_endpoint(request):
        path = request.url.path
        if path.endswith("/agents/wheel-context"):
            return httpx.Response(200, json={"agent": {"agent_name": "wheel-context"}})
        if path.endswith("/agents/wheel-context/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            context_checks.append(json.loads(request.content))
            return httpx.Response(
                200, json={"allowed": True, "confidence": 1, "execution_contract": "hosted-v1"}
            )
        raise AssertionError(f"Unexpected context-wheel route: {path}")

    context_handle = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": [{"target": "context_app.run_dummy"}],
            "checkpoints": {
                "dummy_tool": {
                    "target": "context_app.dummy_tool",
                    "input_from": "all_kwargs",
                    "context_from": {"path": ["params", "b"]},
                }
            },
        },
        agent_name="wheel-context",
        bindings={"context_app": context_app},
        connection={
            "runtime_url": "https://runtime.example.test/api/runtime",
            "agent_key": "wheel-context-key",
            "transport": httpx.MockTransport(context_endpoint),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        try:
            await context_app.run_dummy(a="x", b="private-context-marker")
        except norma.CheckError as exc:
            assert exc.stage == "pre"
            assert "Checkpoint 'dummy_tool' pre-check source 'context_from'" in str(exc)
            assert "/checkpoints/dummy_tool/context_from" in str(exc)
            assert "context_from must resolve to a JSON object" in str(exc)
            assert "private-context-marker" not in str(exc)
            assert exc.__cause__ is None
        else:
            raise AssertionError("Installed context source accepted a scalar value")
        assert context_effects == []
        assert context_checks == []

        assert await context_app.run_dummy(a="x", b={"note": "dynamic-context"}) == (
            "x-{'note': 'dynamic-context'}"
        )
        assert context_effects == ["dummy_tool"]
        assert [body["stage"] for body in context_checks] == ["pre", "post"]
        assert context_checks[0]["step"]["context"]["note"] == "dynamic-context"
    finally:
        await context_handle.aclose()

    receiver_app = GuardedApp()
    receiver_checks = []

    class ReceiverTool:
        def __init__(self):
            self.calls = 0
            self.identity = []

        async def execute(receiver, value):
            receiver.calls += 1
            receiver.identity.append(receiver is receiver_tool)
            return value

    receiver_tool = ReceiverTool()

    async def receiver_run():
        return await receiver_tool.execute("installed-receiver")

    receiver_app.run = receiver_run

    def receiver_endpoint(request):
        path = request.url.path
        if path.endswith("/agents/wheel-receiver"):
            return httpx.Response(200, json={"agent": {"agent_name": "wheel-receiver"}})
        if path.endswith("/agents/wheel-receiver/rules"):
            return httpx.Response(200, json={"rules": []})
        if path.endswith("/checks"):
            receiver_checks.append(json.loads(request.content))
            return httpx.Response(
                200, json={"allowed": True, "confidence": 1, "execution_contract": "hosted-v1"}
            )
        raise AssertionError(f"Unexpected receiver-wheel route: {path}")

    receiver_handle = await norma.aconfigure(
        {
            "version": 2,
            "entrypoints": ["receiver_app.run"],
            "checkpoints": {"execute": {"target": "receiver_tool.execute"}},
        },
        agent_name="wheel-receiver",
        bindings={"receiver_app": receiver_app, "receiver_tool": receiver_tool},
        connection={
            "runtime_url": "https://runtime.example.test/api/runtime",
            "agent_key": "wheel-receiver-key",
            "transport": httpx.MockTransport(receiver_endpoint),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    try:
        assert await receiver_app.run() == "installed-receiver"
        assert receiver_tool.calls == 1
        assert receiver_tool.identity == [True]
        assert [body["step"]["input"] for body in receiver_checks] == [
            {"value": "installed-receiver"},
            {"value": "installed-receiver"},
        ]
        receiver_schema = next(
            schema
            for schema in receiver_handle.schemas["wheel-receiver"]
            if schema.name == "execute"
        )
        assert set(receiver_schema.input_schema["properties"]) == {"value"}
    finally:
        await receiver_handle.aclose()

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
import importlib.util
from contextlib import redirect_stdout
from io import StringIO

stream_example_path = pathlib.Path("STREAM_EXAMPLE_APP_PATH")
stream_example_spec = importlib.util.spec_from_file_location(
    "norma_streamed_finish_example", stream_example_path
)
assert stream_example_spec is not None and stream_example_spec.loader is not None
stream_example = importlib.util.module_from_spec(stream_example_spec)
stream_example_spec.loader.exec_module(stream_example)
stream_config_path = stream_example_path.parent / "config" / (
    os.environ["NORMA_AGENT_NAME"] + ".json"
)
stream_config = json.loads(stream_config_path.read_text())
stream_finish = stream_config["entrypoints"][0]["turn"]["finish"]
assert "stream" in stream_finish and "on_verdict" not in stream_finish
stream_state = {"verdict": "pass"}
stream_requests = []

def streamed_finish_endpoint(request):
    path = request.url.path
    agent_name = os.environ["NORMA_AGENT_NAME"]
    if path.endswith("/agents/" + agent_name):
        return httpx.Response(
            200, json={"agent": {"agent_name": agent_name}, "steps": []}
        )
    if path.endswith("/agents/" + agent_name + "/rules"):
        return httpx.Response(200, json={"rules": []})
    if path.endswith("/checks"):
        body = json.loads(request.content)
        stream_requests.append(body)
        assert body["agent_name"] == agent_name
        assert body["step"]["type"] == "turn"
        verdict = stream_state["verdict"]
        return httpx.Response(
            200,
            json={
                "execution_contract": "hosted-v1",
                "verdict": verdict,
                "reason": "Checked by installed-wheel fixture",
                "reason_codes": [],
                "held_by": [],
                "would_hold_by": [],
                "continuation": {"count": 0, "max": 1 if verdict == "hold" else 2},
                "allow_finish": verdict != "hold",
                "outcome": "held" if verdict == "hold" else "completed",
            },
        )
    raise AssertionError(f"Unexpected streamed-example route: {path}")

async def run_stream_example():
    output = StringIO()
    with redirect_stdout(output):
        await stream_example.main(connection={
            "transport": httpx.MockTransport(streamed_finish_endpoint),
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        })
    return output.getvalue()

async def qualify_stream_example():
    output = await run_stream_example()
    assert output == "Hello, your request\\n"
    stream_state["verdict"] = "hold"
    held_output = await run_stream_example()
    assert "Turn held:" in held_output
    assert "Collected text: Hello, your request" in held_output
    assert len(stream_requests) == 2
    assert [request["step"]["input"]["final_message"] for request in stream_requests] == [
        "Hello, your request", "Hello, your request"
    ]

asyncio.run(qualify_stream_example())
print(json.dumps({"installed_file": norma.__file__, "version": norma.__version__,
                  "hosted_contract": "passed", "control_artifact_probe": "passed",
                  "streamed_finish_example": "passed",
                  "runtime_dependencies_only": True}))
"""
        smoke = smoke.replace(
            '"STREAM_EXAMPLE_APP_PATH"', json.dumps(str(streamed_example / "app.py"))
        )
        subprocess.run([str(python), "-c", smoke], cwd=root, env=runtime_environment, check=True)
    print(json.dumps({"wheel": wheels[0].name, "outside_repository_install": "passed"}))


if __name__ == "__main__":
    main()
