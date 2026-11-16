"""Validate the DSL using node models and the version check from a trusted Dify 1.7.2 checkout."""

import argparse
import ast
import sys
from abc import ABC
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Literal, Optional, Union

from packaging import version
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from verify_dify_workflows import check_graph, load_yaml


MODELS = {
    "start": "StartNodeData",
    "end": "EndNodeData",
    "code": "CodeNodeData",
    "if-else": "IfElseNodeData",
    "http-request": "HttpRequestNodeData",
}


class DefaultValueTypeError(ValueError):
    pass


def symbol(item: ast.stmt) -> str | None:
    if isinstance(item, (ast.ClassDef, ast.FunctionDef)):
        return item.name
    if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
        return item.targets[0].id
    return None


def evaluate(root: Path, namespace: dict, relative_path: str, *names: str) -> None:
    source = root / relative_path
    original = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    selected = [item for item in original.body if symbol(item) in names]
    if {symbol(item) for item in selected} != set(names):
        raise ValueError(f"Dify 1.7.2 schema symbols missing in {relative_path}")
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)


def native_models(root: Path) -> dict:
    module = ModuleType("_biaoshu_dify_172_schema")
    sys.modules[module.__name__] = module
    module.__dict__.update({name: globals()[name] for name in (
        "ABC", "Sequence", "StrEnum", "Any", "Literal", "Optional", "Union", "version", "BaseModel", "Field",
        "ValidationInfo", "field_validator", "model_validator", "DefaultValueTypeError",
    )})
    # Runtime settings supply default values for these model fields. The DSL sets all timeouts explicitly.
    module.dify_config = SimpleNamespace(HTTP_REQUEST_MAX_CONNECT_TIMEOUT=10, HTTP_REQUEST_MAX_READ_TIMEOUT=60,
                                         HTTP_REQUEST_MAX_WRITE_TIMEOUT=20, HTTP_REQUEST_NODE_SSL_VERIFY=True)
    sources = [
        ("core/workflow/nodes/enums.py", "NodeType", "ErrorStrategy"),
        ("core/file/enums.py", "FileType", "FileTransferMethod"),
        ("core/app/app_config/entities.py", "VariableEntityType", "VariableEntity"),
        ("core/helper/code_executor/code_executor.py", "CodeLanguage"),
        ("core/workflow/entities/variable_entities.py", "VariableSelector"),
        ("core/workflow/utils/condition/entities.py", "SupportedComparisonOperator", "SubCondition",
         "SubVariableCondition", "Condition"),
        ("core/workflow/nodes/base/entities.py", "DefaultValueType", "NumberType", "DefaultValue", "RetryConfig",
         "BaseNodeData"),
        ("core/workflow/nodes/start/entities.py", "StartNodeData"),
        ("core/workflow/nodes/end/entities.py", "EndNodeData"),
        ("core/workflow/nodes/code/entities.py", "CodeNodeData"),
        ("core/workflow/nodes/if_else/entities.py", "IfElseNodeData"),
        ("core/workflow/nodes/http_request/entities.py", "HttpRequestNodeAuthorizationConfig",
         "HttpRequestNodeAuthorization", "BodyData", "HttpRequestNodeBody", "HttpRequestNodeTimeout", "HttpRequestNodeData"),
        ("services/app_dsl_service.py", "CURRENT_DSL_VERSION", "ImportStatus", "_check_version_compatibility"),
    ]
    for relative_path, *names in sources:
        evaluate(root, module.__dict__, relative_path, *names)
    if module.CURRENT_DSL_VERSION != "0.3.1":
        raise ValueError("The trusted source checkout does not match the Dify 1.7.2 DSL baseline")
    return module.__dict__


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="local trusted Dify 1.7.2 repository root or its api directory")
    args = parser.parse_args()
    root = args.source / "api" if (args.source / "api").is_dir() else args.source
    models = native_models(root)
    flows, loader = load_yaml()
    for key, flow in flows.items():
        assert models["_check_version_compatibility"](flow["version"]) == models["ImportStatus"].COMPLETED
        for node_id, data in check_graph(flow).items():
            kind = models["NodeType"](data["type"])
            parsed = models[MODELS[kind]].model_validate(data)
            assert parsed.title == data["title"], node_id
        print(f"{key} native schema PASS")
    print(f"Validated {len(flows)} workflows with {loader} and Dify 1.7.2 schema. Live Dify was not used.")


if __name__ == "__main__":
    main()
