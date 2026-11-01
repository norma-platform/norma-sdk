"""Public behavior of the offline instrumentation validator."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_FIXTURE_MANIFEST = Path(__file__).resolve().parents[2] / "schema/validation-fixtures/manifest.json"
_RULE_COVERAGE_FIXTURE = (
    Path(__file__).resolve().parents[2] / "schema/validation-fixtures/rule-coverage.json"
)


def _run_cli(
    *arguments: str, extra_env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("NORMA_")}
    if extra_env is not None:
        environment.update(extra_env)
    return subprocess.run(
        [str(Path(sys.executable).with_name("norma")), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        cwd=cwd,
        check=False,
    )


def test_validate_accepts_one_file_without_hosted_credentials(tmp_path: Path) -> None:
    config = tmp_path / "valid-agent.json"
    config.write_text(
        '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}',
        encoding="utf-8",
    )

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "valid": True,
        "language": "python",
        "files_checked": 1,
        "diagnostics": [],
    }


def test_validate_rejects_typescript_only_all_params_with_capability_path(tmp_path: Path) -> None:
    config = tmp_path / "agent-alpha.json"
    config.write_text(
        '{"version":2,"entrypoints":["app.run"],"checkpoints":{"send":'
        '{"target":"app.send","input_from":"all_params"}}}',
        encoding="utf-8",
    )

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["valid"] is False
    diagnostic = next(
        item for item in report["diagnostics"] if item["path"] == "/checkpoints/send/input_from"
    )
    assert diagnostic["code"] == "capability"
    assert diagnostic["path"] == "/checkpoints/send/input_from"


def test_config_check_does_not_waive_all_params_capability_error(tmp_path: Path) -> None:
    config = tmp_path / "agent-alpha.json"
    config.write_text(
        '{"version":2,"entrypoints":["app.run"],"checkpoints":{"send":'
        '{"target":"app.send","input_from":"all_params"}}}',
        encoding="utf-8",
    )
    rules = tmp_path / "rules.json"
    rules.write_text("[]", encoding="utf-8")

    result = _run_cli(
        "config", "check", str(config), "--rules", str(rules), "--allow-unknown", "--json"
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["valid"] is False
    diagnostic = next(
        item for item in report["diagnostics"] if item["path"] == "/checkpoints/send/input_from"
    )
    assert diagnostic["code"] == "capability"
    assert diagnostic["path"] == "/checkpoints/send/input_from"


def test_validate_aggregates_sorted_directory_errors_without_echoing_values(
    tmp_path: Path,
) -> None:
    (tmp_path / "zeta.json").write_text('{"credential":"private-marker"', encoding="utf-8")
    (tmp_path / "alpha.yaml").write_text("version: 999\n", encoding="utf-8")

    result = _run_cli("config", "validate", str(tmp_path), "--json")

    assert result.returncode == 1
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["valid"] is False
    assert report["language"] == "python"
    assert report["files_checked"] == 2
    diagnostic_files = [item["file"] for item in report["diagnostics"]]
    assert diagnostic_files == sorted(diagnostic_files)
    assert set(diagnostic_files) == {str(tmp_path / "alpha.yaml"), str(tmp_path / "zeta.json")}
    assert all(
        set(item) == {"file", "code", "path", "message", "line", "column"}
        for item in report["diagnostics"]
    )
    assert "private-marker" not in result.stdout


@pytest.mark.parametrize(
    "fixture", json.loads(_FIXTURE_MANIFEST.read_text())["cases"], ids=lambda case: case["name"]
)
def test_validate_matches_shared_language_fixtures(
    tmp_path: Path, fixture: dict[str, object]
) -> None:
    extension = ".json" if fixture["format"] == "json" else ".yaml"
    config = tmp_path / f"fixture-agent{extension}"
    config.write_text(str(fixture["source"]), encoding="utf-8")

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == (0 if fixture["valid"] else 1)
    report = json.loads(result.stdout)
    assert report["valid"] is fixture["valid"]
    if not fixture["valid"]:
        diagnostic = report["diagnostics"][0]
        for key in ("code", "path", "line", "column"):
            if key in fixture:
                assert diagnostic[key] == fixture[key]


@pytest.mark.parametrize(
    "fixture",
    json.loads(_RULE_COVERAGE_FIXTURE.read_text())["cases"],
    ids=lambda case: case["name"],
)
def test_check_matches_shared_rule_coverage_fixtures(
    tmp_path: Path, fixture: dict[str, object]
) -> None:
    config = fixture["config"]
    rules = fixture["rules"]
    assert isinstance(config, dict)
    assert isinstance(rules, dict)
    config_extension = ".json" if config["format"] == "json" else ".yaml"
    rules_extension = ".json" if rules["format"] == "json" else ".yaml"
    config_file = tmp_path / f"fixture-agent{config_extension}"
    rules_file = tmp_path / f"rule-set{rules_extension}"
    config_file.write_text(str(config["source"]), encoding="utf-8")
    rules_file.write_text(str(rules["source"]), encoding="utf-8")
    args = ["config", "check", str(config_file), "--rules", str(rules_file), "--json"]
    if fixture.get("allow_unknown"):
        args.append("--allow-unknown")

    result = _run_cli(*args)

    expected = fixture["expect"]["python"]
    assert result.returncode == (0 if expected["valid"] else 1)
    report = json.loads(result.stdout)
    assert report["valid"] is expected["valid"]
    assert report["coverage_complete"] is expected["coverage_complete"]
    assert all(
        set(item)
        == {
            "file",
            "rule_file",
            "rule_index",
            "rule_path",
            "checkpoint",
            "stage",
            "selector",
            "status",
        }
        for item in report["coverage"]
    )
    assert [
        {key: item[key] for key in ("code", "path")} for item in report["diagnostics"]
    ] == expected["diagnostics"]
    assert [
        {key: item[key] for key in ("rule_path", "checkpoint", "stage", "selector", "status")}
        for item in report["coverage"]
    ] == expected["coverage"]


def test_check_tracks_json_evaluator_field_constraint_dependencies(tmp_path: Path) -> None:
    config = tmp_path / "agent-alpha.json"
    config.write_text(
        '{"version":2,"entrypoints":["app.run"],"checkpoints":{"send":'
        '{"target":"app.send","input_from":{"value":{"recipient":{"email":"a"}}}}}}',
        encoding="utf-8",
    )
    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "condition": {
                            "selector": {"path": "input"},
                            "evaluator": {
                                "name": "json",
                                "config": {
                                    "field_types": {"recipient.email": "string"},
                                    "field_constraints": {"recipient.email": {"min": 1}},
                                },
                            },
                        },
                        "scope": {"stages": ["pre"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli("config", "check", str(config), "--rules", str(rules), "--json")

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert [
        (item["rule_path"], item["selector"], item["status"]) for item in report["coverage"]
    ] == [
        (
            "/rules/0/condition/evaluator/config/field_constraints/recipient.email",
            "input.recipient.email",
            "covered",
        ),
        (
            "/rules/0/condition/evaluator/config/field_types/recipient.email",
            "input.recipient.email",
            "covered",
        ),
        ("/rules/0/condition/selector/path", "input", "covered"),
    ]


@pytest.mark.parametrize(
    ("input_value", "field_path", "expected_status"),
    [
        ({"recipient": None}, "recipient", "covered"),
        ({"recipients": [{"name": "A"}]}, "recipients.0.name", "missing"),
        ({"recipient": {"0": "A"}}, "recipient.0", "covered"),
    ],
)
def test_json_field_type_dependencies_follow_dictionary_only_dot_paths(
    tmp_path: Path,
    input_value: dict[str, object],
    field_path: str,
    expected_status: str,
) -> None:
    config = tmp_path / "agent-alpha.json"
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "entrypoints": ["app.run"],
                "checkpoints": {
                    "send": {"target": "app.send", "input_from": {"value": input_value}}
                },
            }
        ),
        encoding="utf-8",
    )
    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "condition": {
                            "selector": {"path": "input"},
                            "evaluator": {
                                "name": "json",
                                "config": {"field_types": {field_path: "string"}},
                            },
                        },
                        "scope": {"stages": ["pre"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli("config", "check", str(config), "--rules", str(rules), "--json")

    report = json.loads(result.stdout)
    assert report["coverage"][0]["selector"] == f"input.{field_path}"
    assert report["coverage"][0]["status"] == expected_status


@pytest.mark.parametrize("version", [[], {}, "secret-version"])
def test_validate_reports_malformed_version_without_echoing_it(
    tmp_path: Path, version: object
) -> None:
    config = tmp_path / "version-agent.json"
    config.write_text(
        json.dumps({"version": version, "entrypoints": ["app.run"], "checkpoints": {}}),
        encoding="utf-8",
    )

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["diagnostics"][0]["code"] == "schema"
    assert report["diagnostics"][0]["path"] == "/version"
    assert "secret-version" not in result.stdout


@pytest.mark.parametrize("contents", ["[]", "null"])
def test_validate_classifies_non_object_document_roots_as_schema_errors(
    tmp_path: Path, contents: str
) -> None:
    config = tmp_path / "root-agent.json"
    config.write_text(contents, encoding="utf-8")

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 1
    diagnostic = json.loads(result.stdout)["diagnostics"][0]
    assert diagnostic["code"] == "schema"
    assert diagnostic["path"] == ""


def test_validate_reports_python_capability_and_selector_errors(tmp_path: Path) -> None:
    config = tmp_path / "valid-agent.json"
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "entrypoints": [
                    {
                        "target": "app.run",
                        "parameter_names": ["request"],
                        "turn": {
                            "session_id": {"path": ["result", "id"]},
                            "turn_id": {"value": "turn-1"},
                        },
                    }
                ],
                "checkpoints": {"send": {"target": "app.send", "parameter_names": ["text"]}},
                "connection": {"http_client": {"binding": "app.client"}},
                "operations": {
                    "send": {
                        "target": "app.send",
                        "operation_key": {"value": "request-id"},
                        "parameter_names": ["request"],
                        "returns": "promise",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 1
    diagnostics = json.loads(result.stdout)["diagnostics"]
    assert {item["code"] for item in diagnostics} == {"capability", "selector"}
    assert {item["path"] for item in diagnostics} == {
        "/checkpoints/send/parameter_names",
        "/connection/http_client",
        "/entrypoints/0/turn/session_id/path",
        "/entrypoints/0/parameter_names",
        "/operations/send/parameter_names",
        "/operations/send/returns",
    }


def test_validate_points_schema_typo_to_offending_field(tmp_path: Path) -> None:
    config = tmp_path / "schema-typo-agent.json"
    config.write_text(
        '{"version":2,"entrypoints":[{"target":"app.run",'
        '"parametr_names":["request"]}],"checkpoints":{}}',
        encoding="utf-8",
    )

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 1
    diagnostic = json.loads(result.stdout)["diagnostics"][0]
    assert diagnostic["code"] == "schema"
    assert diagnostic["path"] == "/entrypoints/0/parametr_names"


def test_validate_reports_one_context_union_branch_and_deduplicates_typo(
    tmp_path: Path,
) -> None:
    config = tmp_path / "context-union-agent.json"
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "entrypoints": [{"target": "app.run", "context": {"value": "safe", "vale": "x"}}],
                "checkpoints": {},
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 1
    diagnostics = json.loads(result.stdout)["diagnostics"]
    assert [(item["path"], item["message"]) for item in diagnostics] == [
        ("/entrypoints/0/context/vale", "The object contains an unsupported field.")
    ]


def test_validate_reports_unknown_only_for_context_union_without_discriminator(
    tmp_path: Path,
) -> None:
    config = tmp_path / "invalid-union-agent.yaml"
    config.write_text(
        "version: 2\nentrypoints:\n  - target: app.run\n    context:\n      vale: x\n"
        "checkpoints: {}\n",
        encoding="utf-8",
    )

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 1
    diagnostics = json.loads(result.stdout)["diagnostics"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "schema"
    assert diagnostics[0]["path"] == "/entrypoints/0/context/vale"
    assert diagnostics[0]["message"] == "The object contains an unsupported field."


@pytest.mark.parametrize("over_limit", [False, True])
def test_validate_checkpoint_state_matches_runtime_size_limit(
    tmp_path: Path, over_limit: bool
) -> None:
    from norma import parse_instrumentation

    state_text_size = 65_529 if over_limit else 65_528
    config_value = {
        "version": 1,
        "entrypoints": ["app.run"],
        "checkpoints": {"send": {"target": "app.send", "state": {"x": "a" * state_text_size}}},
    }
    source = json.dumps(config_value, separators=(",", ":"))
    config = tmp_path / "state-limit-agent.json"
    config.write_text(source, encoding="utf-8")

    result = _run_cli("config", "validate", str(config), "--json")

    if over_limit:
        with pytest.raises(ValueError, match="Invalid state for checkpoint"):
            parse_instrumentation(source)
        assert result.returncode == 1
        diagnostic = json.loads(result.stdout)["diagnostics"][0]
        assert diagnostic["code"] == "schema"
        assert diagnostic["path"] == "/checkpoints/send/state"
    else:
        parsed = parse_instrumentation(source)
        assert parsed["checkpoints"]["send"]["state"]["x"] == "a" * state_text_size
        assert result.returncode == 0


def test_validate_catches_python_only_adapter_and_action_selector_limits(tmp_path: Path) -> None:
    config = tmp_path / "adapter-limits-agent.json"
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "entrypoints": [
                    {
                        "target": "app.run",
                        "context": {"path": ["context", "request"]},
                        "turn": {"session_id": {"value": "session"}, "turn_id": {"value": "turn"}},
                    }
                ],
                "checkpoints": {
                    "send": {
                        "target": "app.send",
                        "options": {
                            "stepName": "send",
                            "stepType": "tool",
                            "action_kind": "shell",
                        },
                        "action": {"kind": "shell", "target": {"path": ["result", "target"]}},
                    }
                },
                "adapters": {
                    "agent": {
                        "kind": "openai_agents",
                        "agent": {"binding": "app.agent"},
                        "tools": {
                            "search": {
                                "description": "search",
                                "parameters": {},
                                "execute": {"binding": "app.execute"},
                            },
                            "native": {
                                "tool": {"binding": "app.native_tool"},
                                "action": {
                                    "kind": "shell",
                                    "target": {"path": ["params", "destination"]},
                                },
                            },
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 1
    diagnostics = json.loads(result.stdout)["diagnostics"]
    assert {diagnostic["code"] for diagnostic in diagnostics} == {"capability", "selector"}
    assert {diagnostic["path"] for diagnostic in diagnostics} == {
        "/adapters/agent/tools/search",
        "/adapters/agent/tools/native/action/target/path",
        "/checkpoints/send/action/target/path",
        "/checkpoints/send/options/stepName",
        "/checkpoints/send/options/stepType",
        "/entrypoints/0/context/path",
    }


@pytest.mark.parametrize(
    ("source_name", "contents", "code"),
    [
        ("empty-directory", None, "io"),
        ("short.json", '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}', "identity"),
        ("unsupported.txt", "not a config", "io"),
    ],
)
def test_validate_reports_empty_directory_bad_stem_and_unsupported_extension(
    tmp_path: Path, source_name: str, contents: str | None, code: str
) -> None:
    source = tmp_path / source_name
    if contents is None:
        source.mkdir()
    else:
        source.write_text(contents, encoding="utf-8")

    result = _run_cli("config", "validate", str(source), "--json")

    assert result.returncode == 1
    diagnostic = json.loads(result.stdout)["diagnostics"][0]
    assert diagnostic["code"] == code


def test_validate_detects_json_yaml_duplicate_agent_stem(tmp_path: Path) -> None:
    content = '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}'
    (tmp_path / "duplicate-agent.json").write_text(content, encoding="utf-8")
    (tmp_path / "duplicate-agent.yaml").write_text(
        "version: 1\nentrypoints: [app.run]\ncheckpoints: {}\n", encoding="utf-8"
    )

    result = _run_cli("config", "validate", str(tmp_path), "--json")

    assert result.returncode == 1
    assert any(
        diagnostic["code"] == "identity" for diagnostic in json.loads(result.stdout)["diagnostics"]
    )


@pytest.mark.parametrize(
    "contents",
    [
        (
            "version: 2\nentrypoints: [app.run]\ncheckpoints:\n  send:\n"
            "    target: app.send\n    state:\n      value: .nan\n"
        ),
        (
            '{"version":2,"entrypoints":[{"target":"app.run","turn":'
            '{"session_id":{"value":"session"},"turn_id":{"value":"turn"},'
            '"application":"one","platform":"two"}}],"checkpoints":{}}'
        ),
        (
            "version: 2\nentrypoints: [app.run]\ncheckpoints:\n  send:\n"
            "    target: app.send\n    state: &recursive\n      self: *recursive\n"
        ),
    ],
)
def test_validate_preserves_strict_parser_static_rejections(tmp_path: Path, contents: str) -> None:
    config = tmp_path / "strict-agent.yaml"
    config.write_text(contents, encoding="utf-8")

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["valid"] is False
    assert any(item["code"] == "schema" for item in report["diagnostics"])
    assert "session" not in result.stdout
    assert "one" not in result.stdout


def test_validate_matches_existing_parser_version_number_semantics(tmp_path: Path) -> None:
    config = tmp_path / "numeric-version.json"
    config.write_text('{"version":1.0,"entrypoints":["app.run"],"checkpoints":{}}')

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 0
    assert json.loads(result.stdout)["valid"] is True


def test_validate_reports_yaml_syntax_position(tmp_path: Path) -> None:
    config = tmp_path / "yaml-agent.yaml"
    config.write_text("version: 2\nentrypoints: [app.run\ncheckpoints: {}\n", encoding="utf-8")

    result = _run_cli("config", "validate", str(config), "--json")

    assert result.returncode == 1
    diagnostic = json.loads(result.stdout)["diagnostics"][0]
    assert diagnostic["code"] == "syntax"
    assert diagnostic["line"] == 3
    assert diagnostic["column"] is not None


def test_validate_human_output_keeps_invalid_diagnostics_on_stderr(tmp_path: Path) -> None:
    config = tmp_path / "valid-agent.json"
    config.write_text('{"version":1,"entrypoints":["app.run"],"checkpoints":{}}', encoding="utf-8")

    valid = _run_cli("config", "validate", str(config))
    invalid = _run_cli("config", "validate", str(tmp_path / "missing.json"))

    assert valid.returncode == 0
    assert "offline" in valid.stdout
    assert "Bindings and authorization remain startup checks." in valid.stdout
    assert valid.stderr == ""
    assert invalid.returncode == 1
    assert invalid.stdout == ""
    assert "[io]" in invalid.stderr


def test_validate_with_bindings_and_env_references_stays_offline(tmp_path: Path) -> None:
    (tmp_path / "sitecustomize.py").write_text(
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
    config = tmp_path / "offline-agent.json"
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "entrypoints": [
                    {
                        "target": "application_sentinel.run",
                        "turn": {
                            "session_id": {"value": "session"},
                            "turn_id": {"value": "turn"},
                        },
                    }
                ],
                "checkpoints": {
                    "step": {
                        "target": "application_sentinel.step",
                        "state_from": {"provider": "application_sentinel.providers.state"},
                    }
                },
                "connection": {
                    "runtime_url": {"env": "NORMA_RUNTIME_URL"},
                    "agent_key": {"env": "NORMA_AGENT_KEY"},
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli(
        "config",
        "validate",
        str(config),
        "--json",
        extra_env={"PYTHONPATH": str(tmp_path)},
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["valid"] is True
    assert result.stderr == ""


def test_check_with_local_rules_stays_offline_and_does_not_import_sdk(
    tmp_path: Path,
) -> None:
    (tmp_path / "sitecustomize.py").write_text(
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
    config = tmp_path / "offline-agent.json"
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "entrypoints": ["application_sentinel.run"],
                "checkpoints": {
                    "send": {
                        "target": "application_sentinel.send",
                        "input_from": {"value": {"recipient": "person@example.test"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    rules = tmp_path / "local-rules.json"
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "condition": {
                            "selector": {"path": "input.recipient"},
                            "evaluator": {"name": "json", "config": {}},
                        },
                        "scope": {"stages": ["pre"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli(
        "config",
        "check",
        str(config),
        "--rules",
        str(rules),
        "--json",
        extra_env={"PYTHONPATH": str(tmp_path)},
    )

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["coverage_complete"] is True
    assert report["rules_checked"] == 1
    assert result.stderr == ""


def test_check_matches_rule_directory_by_exact_stem_without_reading_other_entries(
    tmp_path: Path,
) -> None:
    config = tmp_path / "agent-one-long.json"
    config.write_text(
        '{"version":2,"entrypoints":["app.run"],"checkpoints":{"send":'
        '{"target":"app.send","input_from":{"value":{"to":"person@example.test"}}}}}',
        encoding="utf-8",
    )
    rule_directory = tmp_path / "rules"
    rule_directory.mkdir()
    (rule_directory / "agent-one-long.yaml").write_text(
        "rules:\n"
        "  - condition:\n"
        "      selector: {path: input.to}\n"
        "      evaluator: {name: json, config: {}}\n"
        "    scope: {stages: [pre]}\n",
        encoding="utf-8",
    )
    # A nonmatching directory entry must not be parsed.
    (rule_directory / "unused.yaml").write_text("rules: [\n", encoding="utf-8")

    result = _run_cli("config", "check", str(config), "--rules", str(rule_directory), "--json")

    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["rules_checked"] == 1
    assert len(report["coverage"]) == 1
    assert report["coverage"][0]["checkpoint"] == "send"


def test_check_rejects_duplicate_keys_inside_console_draft_json_text(tmp_path: Path) -> None:
    config = tmp_path / "agent-one-long.json"
    config.write_text(
        '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}',
        encoding="utf-8",
    )
    rules = tmp_path / "draft.json"
    rules.write_text(
        json.dumps(
            {
                "agentName": "agent-one-long",
                "jsonText": '{"condition":{},"condition":{},"action":{}}',
                "previousDraft": {"jsonText": "ignored"},
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli("config", "check", str(config), "--rules", str(rules), "--json")

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["diagnostics"][0]["code"] == "rules_schema"
    assert report["diagnostics"][0]["path"] == "/jsonText"
    assert "ignored" not in result.stdout


def test_check_reports_malformed_yaml_rules_without_traceback(tmp_path: Path) -> None:
    config = tmp_path / "agent-one-long.json"
    config.write_text(
        '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}',
        encoding="utf-8",
    )
    rules = tmp_path / "rules.yaml"
    rules.write_text("rules:\n  - condition: [\n", encoding="utf-8")

    result = _run_cli("config", "check", str(config), "--rules", str(rules), "--json")

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["diagnostics"][0]["code"] == "syntax"
    assert report["diagnostics"][0]["line"] is not None
    assert "Traceback" not in result.stdout + result.stderr


def test_validate_ignores_invalid_sdk_settings_and_project_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "NORMA_OBSERVABILITY_ENABLED=poison-dotenv-setting\n", encoding="utf-8"
    )
    config = tmp_path / "isolated-agent.json"
    config.write_text('{"version":1,"entrypoints":["app.run"],"checkpoints":{}}')

    result = _run_cli(
        "config",
        "validate",
        str(config),
        "--json",
        extra_env={"NORMA_OBSERVABILITY_ENABLED": "poison-env-setting"},
        cwd=tmp_path,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["valid"] is True
    assert "poison-" not in result.stdout + result.stderr

    legacy = subprocess.run(
        [sys.executable, "-m", "norma.cli", "version"],
        capture_output=True,
        text=True,
        env={
            **{key: value for key, value in os.environ.items() if not key.startswith("NORMA_")},
            "NORMA_OBSERVABILITY_ENABLED": "poison-env-setting",
        },
        cwd=tmp_path,
        check=False,
    )
    assert legacy.returncode != 0
