# Modified for Norma.
"""Qualify the built wheel with the official Agents Runner outside the checkout."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def main() -> None:
    package = Path(__file__).resolve().parents[1]
    sdk_version = tomllib.loads((package / "pyproject.toml").read_text())["project"]["version"]
    expected_wheel = f"norma_sdk-{sdk_version}-py3-none-any.whl"
    wheels = [
        wheel
        for wheel in (package / "dist").glob("norma_sdk-*.whl")
        if wheel.name == expected_wheel
    ]
    if len(wheels) != 1:
        raise SystemExit(f"Expected current SDK wheel {expected_wheel} in dist/")
    agents_version = sys.argv[1] if len(sys.argv) > 1 else "0.22.3"
    prefixes = (
        "OPENAI_",
        "ANTHROPIC_",
        "GOOGLE_",
        "GEMINI_",
        "AZURE_",
        "AWS_",
        "OTEL_",
        "LANGSMITH_",
        "NORMA_",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(prefixes) and key != "PYTHONPATH"
    }
    environment["PYTHONNOUSERSITE"] = "1"
    environment["AWS_ACCESS_KEY_ID"] = "testing"
    environment["AWS_SECRET_ACCESS_KEY"] = "testing"
    environment["AWS_DEFAULT_REGION"] = "us-east-1"
    environment["AWS_EC2_METADATA_DISABLED"] = "true"
    with tempfile.TemporaryDirectory(prefix="norma-real-agents-") as directory:
        root = Path(directory)
        wheel = root / wheels[0].name
        shutil.copy2(wheels[0], wheel)
        shutil.copy2(package / "tests/test_openai_agents.py", root / "test_openai_agents.py")
        shutil.copy2(
            package / "tests/test_instrumentation_native_finish.py",
            root / "test_instrumentation_native_finish.py",
        )
        instrumentation_tests = root / "python" / "tests"
        instrumentation_tests.mkdir(parents=True)
        shutil.copy2(
            package / "tests/test_instrumentation.py",
            instrumentation_tests / "test_instrumentation.py",
        )
        shutil.copytree(package.parent / "schema", root / "schema")
        subprocess.run([sys.executable, "-m", "venv", str(root / "env")], check=True)
        python = str(root / "env/bin/python")
        subprocess.run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--index-url",
                "https://pypi.org/simple",
                str(wheel),
                f"openai-agents=={agents_version}",
                "pydantic-ai-slim==2.59.0",
                "strands-agents==1.61.1",
                "pytest",
                "pytest-asyncio",
            ],
            check=True,
            cwd=root,
            env=environment,
        )
        print(f"Wheel SHA256: {hashlib.sha256(wheel.read_bytes()).hexdigest()}", flush=True)
        bootstrap = """
import sys
from pathlib import Path

def forbid_network(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.bind"):
        raise AssertionError("Unexpected network operation: " + event)
sys.addaudithook(forbid_network)
import importlib.metadata
import norma
import pytest
assert Path(norma.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
print("OpenAI Agents:", importlib.metadata.version("openai-agents"))
print("Installed Norma:", norma.__file__)
for arguments in (
    ["test_openai_agents.py", "-q"],
    ["test_instrumentation_native_finish.py", "-q"],
    ["python/tests/test_instrumentation.py", "-k", "configured_strands_adapter", "-q"],
):
    result = pytest.main(arguments)
    if result != pytest.ExitCode.OK:
        raise SystemExit(result)
"""
        subprocess.run([python, "-c", bootstrap], check=True, cwd=root, env=environment)


if __name__ == "__main__":
    main()
