# Modified for Norma.
"""Install and exercise the built wheel outside any source checkout."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile
from email.parser import BytesParser


def main() -> None:
    package = Path(__file__).resolve().parents[1]
    wheels = list((package / "dist").glob("norma_sdk-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one built SDK wheel in dist/")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        roots = {name.split("/")[0] for name in names}
        expected = {"norma", wheels[0].name.removesuffix("-py3-none-any.whl") + ".dist-info"}
        if roots != expected:
            raise SystemExit(f"Unexpected distribution namespaces: {roots}")
        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
        deps = metadata.get_all("Requires-Dist", [])
        base = {dependency.split(";")[0].split(">")[0].split("<")[0].split("=")[0]
                for dependency in deps if "extra ==" not in dependency}
        if base != {"httpx", "pydantic", "pydantic-settings", "docstring-parser"}:
            raise SystemExit(f"Unexpected runtime dependencies: {base}")
        if not any(name.endswith("/NOTICE") for name in names):
            raise SystemExit("NOTICE is missing from the distribution")
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
import asyncio, importlib.util, json, pathlib, sys
import httpx
import norma
from norma import NormaClient, Step, checks
from norma.models import Decision
assert pathlib.Path(norma.__file__).is_relative_to(pathlib.Path(sys.prefix))
assert importlib.util.find_spec("agents") is None
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
from norma.models.errors import ErrorCode, make_public_error_code
from norma.errors import NormaAPIError
import importlib
rule_codes = (
    "RULE_NOT_FOUND", "RULE_VERSION_NOT_FOUND", "RULE_BINDING_NOT_FOUND",
    "RULE_NAME_CONFLICT", "RULE_BINDING_CONFLICT", "RULE_IN_USE", "RULE_TEMPLATE_CONFLICT",
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
        assert set(decision.model_dump()) == {"allowed", "reason", "checks", "execution_contract", "recovery_feedback"}
    assert len(seen) == 1
    assert seen[0].url.path == "/api/runtime/api/v1/checks"
    assert json.loads(seen[0].content)["execution_mode"] == "hosted"
asyncio.run(main())
print(json.dumps({"installed_file": norma.__file__, "version": norma.__version__,
                  "hosted_contract": "passed", "runtime_dependencies_only": True}))
'''
        subprocess.run([str(python), "-c", smoke], cwd=root, env=environment, check=True)
    print(json.dumps({"wheel": wheels[0].name, "outside_repository_install": "passed"}))


if __name__ == "__main__":
    main()
