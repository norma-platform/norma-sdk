# Modified for Norma.
"""Check the standalone hosted client allowlist without a private server schema."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
contract = json.loads((ROOT / "public-contract.json").read_text())
actual = []
for file in sorted((ROOT / "src/generated/funcs").glob("*.ts")):
    source = file.read_text()
    actual.append({"file": str(file.relative_to(ROOT)),
                   "method": re.search(r'method: "(\w+)"', source)[1],
                   "path": re.search(r'pathToFunc\(\s*"([^"]+)"', source)[1]})
if actual != contract["generatedRoutes"]:
    raise SystemExit("Hosted route allowlist changed; review public-contract.json")
operation_source = (ROOT / "src/internal/operations.ts").read_text()
expressions = {
    '"/api/v1/operations"': "/api/v1/operations",
    "this.path(operationId)": "/api/v1/operations/{operation_id}",
    "`${this.path(operationId)}/observations`": "/api/v1/operations/{operation_id}/observations",
    "`${this.path(operationId)}/verification`": "/api/v1/operations/{operation_id}/verification",
}
operation_routes = []
for method, expression in re.findall(r'this.request\("(GET|POST)", ([^,]+),', operation_source):
    if expression not in expressions:
        raise SystemExit("Unreviewed operation route expression")
    operation_routes.append(method + " " + expressions[expression])
if sorted(operation_routes) != sorted(contract["operationRoutes"]):
    raise SystemExit("Operation route allowlist changed")
finish_source = (ROOT / "src/finish.ts").read_text()
if 'new URL("api/v1/checks"' not in finish_source or contract["finishRoute"] != "POST /api/v1/checks":
    raise SystemExit("Finish route allowlist changed")
metadata = json.loads((ROOT / "package.json").read_text())
if sorted(metadata["dependencies"]) != contract["dependencies"]:
    raise SystemExit("Unreviewed runtime dependency")
if metadata["version"] not in (ROOT / "src/generated/lib/config.ts").read_text():
    raise SystemExit("SDK transport version differs from package version")
for file in (ROOT / "src").rglob("*.ts"):
    for specifier in re.findall(r'^\s*(?:import|export)\s+(?:[^;]*?from\s*)?["\']([^"\']+)', file.read_text(), re.M):
        if specifier.startswith(".") and not (file.parent / specifier).resolve().is_relative_to(ROOT / "src"):
            raise SystemExit("Source import escapes standalone SDK")
        if specifier == "@openai/agents" and file == ROOT / "src/openai-agents.ts":
            continue
        if not specifier.startswith((".", "node:", "zod")):
            raise SystemExit("Unreviewed source dependency")
print(f"Hosted public contract passed: {len(actual)} generated routes; zod-only runtime dependency")
