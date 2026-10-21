# Modified for Norma.
"""Qualify the built wheel with the official Agents Runner outside the checkout."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    package = Path(__file__).resolve().parents[1]
    wheels = list((package / "dist").glob("norma_sdk-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one built wheel")
    version = sys.argv[1] if len(sys.argv) > 1 else "0.22.3"
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
    with tempfile.TemporaryDirectory(prefix="norma-real-agents-") as directory:
        root = Path(directory)
        wheel = root / wheels[0].name
        shutil.copy2(wheels[0], wheel)
        shutil.copy2(package / "tests/test_openai_agents.py", root / "test_openai_agents.py")
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
                f"openai-agents=={version}",
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
raise SystemExit(pytest.main(["test_openai_agents.py", "-q"]))
"""
        subprocess.run([python, "-c", bootstrap], check=True, cwd=root, env=environment)


if __name__ == "__main__":
    main()
