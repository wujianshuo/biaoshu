"""Generate the committed Dify Workflow DSL files and check their node contracts."""

import argparse
import json
import re
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "workflows"
TARGET = "BIAOSHU_API_BASE"
TOKEN = "BIAOSHU_API_TOKEN"
WORKFLOWS = {
    "WF01": ("项目资料入库", "创建项目，导入或查询解析后的项目资料并返回资料清单。",
             {"project_name": "string", "industry": "string", "specialty": "string", "region": "string", "document_ids": "id_list",
              "document_kinds": "document_kinds"}, 120),
    "WF02": ("招标文件解析", "解析指定版本的招标材料，生成带来源的解析结果供人工确认。",
             {"document_ids": "id_list"}, 240),
    "WF03": ("招标要求分析", "从已审核的招标解析结果生成要求响应矩阵。",
             {}, 60),
    "WF04": ("评标办法分析", "从已审核的招标解析结果生成评分响应矩阵。",
             {}, 60),
    "WF05": ("企业资料匹配", "按适用范围、审核状态和有效期查询企业证据。",
             {"as_of": "date"}, 60),
    "WF06": ("技术标编制", "按已审核的项目数据、评分和知识资料编制并保存技术标章节。",
             {"chapter_ids": "id_list"}, 420),
    "WF09": ("投标文件审查", "审查响应、引用、缺项、有效性及章节一致性。",
             {}, 120),
    "WF10": ("文件组装", "组装技术标 Word 草稿或满足复核门槛的成果。",
             {"mode": "mode", "template_document_id": "string", "include_pdf": "bool"}, 120),
}

REQUEST_CODE = '''from datetime import date
import json
import re
from urllib.parse import urlsplit


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("请求 JSON 存在重复的字段")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("请求 JSON 不支持非有限数值")


def main(project_id: str, request_json: str, api_base: str) -> dict:
    base = (api_base or "").strip().rstrip("/")
    uri = urlsplit(base)
    if uri.scheme not in ("http", "https") or not uri.hostname or uri.username is not None or uri.query or uri.fragment:
        raise ValueError("请在 Dify 环境变量填写内网 BIAOSHU_API_BASE，例如 http://biaoshu-api:8091")
    if any(char.isspace() for char in base):
        raise ValueError("BIAOSHU_API_BASE 不能包含空白字符")
    project_id = (project_id or "").strip()
    if (project_id or PROJECT_REQUIRED) and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project_id):
        raise ValueError("请填写可用的 project_id")
    try:
        payload = json.loads((request_json or "").strip() or "{}",
                             object_pairs_hook=unique_object, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"request_json 不是有效 JSON，第 {exc.lineno} 行第 {exc.colno} 列") from None
    if not isinstance(payload, dict):
        raise ValueError("request_json 必须是 JSON 对象，例如 {}")
    if any(key not in FIELDS for key in payload):
        raise ValueError("request_json 有不支持的字段，请查看工作流输入说明")
    for key, value in payload.items():
        kind = FIELDS[key]
        if kind == "id_list" and (not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        )):
            raise ValueError(f"{key} 必须是非空 ID 字符串组成的数组")
        if kind == "string" and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{key} 必须是非空字符串")
        if kind == "document_kinds" and (not isinstance(value, dict) or any(
            not name.strip() or category not in ("tender", "amendment", "drawing", "boq", "template", "other")
            for name, category in value.items()
        )):
            raise ValueError("document_kinds 必须以文件名为键，值为招标资料类别")
        if kind == "date":
            try:
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                    raise ValueError
                date.fromisoformat(value)
            except ValueError:
                raise ValueError(f"{key} 必须是 yyyy-mm-dd 格式的有效日期") from None
        if kind == "mode" and value not in ("draft", "reviewed"):
            raise ValueError("mode 仅可填写 draft 或 reviewed")
        if kind == "bool" and type(value) is not bool:
            raise ValueError(f"{key} 应填写 true 或 false")
    return {
        "url_base": base,
        "project_id": project_id,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "request_body": json.dumps({**payload, "project_id": project_id or None}, ensure_ascii=False),
    }
'''

RESPONSE_CODE = '''import json


def main(body: str, http_status: int) -> dict:
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        value = None
    if not 200 <= http_status < 300:
        status = "backend_error"
    elif not isinstance(value, dict):
        status = "invalid_response"
    else:
        label = value.get("status")
        status = label.strip()[:120] if isinstance(label, str) and label.strip() else "ok"
    return {"status": status}
'''


def stable_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, "https://github.com/wujianshuo/biaoshu/dify/" + label))


def selector(node_id: str, output: str) -> str:
    return f"{{{{#{node_id}.{output}#}}}}"


def node(node_id: str, kind: str, title: str, x: int, y: int = 100, **fields: Any) -> dict:
    location = {"x": x, "y": y}
    return {
        "id": node_id,
        "type": "custom",
        "position": location,
        "positionAbsolute": dict(location),
        "sourcePosition": "right",
        "targetPosition": "left",
        "width": 244,
        "height": 90,
        "selected": False,
        "data": {"type": kind, "title": title, "desc": "", "selected": False, **fields},
    }


def edge(source: dict, target: dict, source_handle: str = "source") -> dict:
    return {
        "id": f"{source['id']}-{source_handle}-{target['id']}-target",
        "source": source["id"],
        "sourceHandle": source_handle,
        "target": target["id"],
        "targetHandle": "target",
        "type": "custom",
        "zIndex": 0,
        "data": {"sourceType": source["data"]["type"], "targetType": target["data"]["type"],
                 "isInIteration": False, "isInLoop": False},
    }


def code_node(node_id: str, title: str, x: int, y: int, code: str, inputs: dict, outputs: dict) -> dict:
    return node(node_id, "code", title, x, y, code_language="python3", code=code,
                variables=[{"variable": key, "value_selector": value} for key, value in inputs.items()],
                outputs={key: {"type": value, "children": None} for key, value in outputs.items()})


def text_part(label: str, key: str, source: str) -> dict:
    return {"id": stable_id(label), "key": key, "type": "text", "value": source, "file": []}


def http_node(key: str, x: int, y: int, timeout: int, upload: bool) -> dict:
    name = f"http_{'upload' if upload else 'run'}"
    route = f"/v1/workflows/{key}/{'upload' if upload else 'run'}"
    if upload:
        body = {"type": "form-data", "data": [
            text_part(key + "_project", "project_id", selector("prepare", "project_id")),
            text_part(key + "_payload", "request_json", selector("prepare", "payload_json")),
            {"id": stable_id(key + "_files"), "key": "files", "type": "file", "value": "",
             "file": ["start", "files"]},
        ]}
        title = "上传并建立项目资料档案"
    else:
        body = {"type": "json", "data": [text_part(key + "_request", "", selector("prepare", "request_body"))]}
        title = "调用内网业务工作流"
    return node(name, "http-request", title, x, y, method="post", url=selector("prepare", "url_base") + route,
                headers="Accept: application/json", params="", body=body,
                authorization={"type": "api-key", "config": {"type": "bearer", "header": "Authorization",
                                                               "api_key": selector("env", TOKEN)}},
                timeout={"connect": 10, "read": timeout, "write": 60}, ssl_verify=True,
                retry_config={"retry_enabled": False, "max_retries": 0, "retry_interval": 1000})


def result_nodes(suffix: str, y: int, response_id: str) -> tuple[dict, dict]:
    status_node = code_node("status_" + suffix, "识别业务状态", 1440, y, RESPONSE_CODE,
                            {"body": [response_id, "body"], "http_status": [response_id, "status_code"]},
                            {"status": "string"})
    outputs = [
        {"variable": "result_json", "value_type": "string", "value_selector": [response_id, "body"]},
        {"variable": "status", "value_type": "string", "value_selector": [status_node["id"], "status"]},
        {"variable": "http_status", "value_type": "number", "value_selector": [response_id, "status_code"]},
    ]
    return status_node, node("end_" + suffix, "end", "输出执行结果", 1760, y, outputs=outputs)


def definition(key: str) -> dict:
    name, description, fields, timeout = WORKFLOWS[key]
    supports_upload = key == "WF01"
    variables = [
        {"label": "项目 ID（WF01 新建项目时留空）" if supports_upload else "项目 ID", "variable": "project_id",
         "type": "text-input", "max_length": 100, "required": not supports_upload, "options": [], "default": ""},
        {"label": "工作流参数 JSON", "variable": "request_json", "type": "paragraph", "max_length": 12000,
         "required": False, "options": [], "default": "{}"},
    ]
    if supports_upload:
        variables.append({"label": "项目文件（可选）", "variable": "files", "type": "file-list", "required": False,
                          "allowed_file_upload_methods": ["local_file"],
                          "allowed_file_types": ["document", "image", "custom"],
                          "allowed_file_extensions": [".PDF", ".DOCX", ".XLSX", ".TXT", ".DWG", ".DXF",
                                                      ".PNG", ".JPG", ".JPEG"],
                          "max_length": 10, "options": []})
    start = node("start", "start", "项目和执行参数", 70, variables=variables)
    code = f"PROJECT_REQUIRED = {not supports_upload!r}\nFIELDS = {fields!r}\n\n" + REQUEST_CODE
    prepare = code_node("prepare", "校验输入并生成请求", 390, 100, code,
                        {"project_id": ["start", "project_id"], "request_json": ["start", "request_json"],
                         "api_base": ["env", TARGET]},
                        {"url_base": "string", "project_id": "string", "payload_json": "string", "request_body": "string"})
    nodes = [start, prepare]
    edges = [edge(start, prepare)]
    if supports_upload:
        case_id = "true"
        choose = node("choose", "if-else", "有项目文件？", 710, cases=[
            {"case_id": case_id, "id": case_id, "logical_operator": "and", "conditions": [
                {"id": stable_id(key + "_route_upload"), "variable_selector": ["start", "files"],
                 "comparison_operator": "not empty", "value": "", "varType": "array[file]"},
            ]},
        ])
        nodes.append(choose)
        edges.append(edge(prepare, choose))
        branches = [(True, 0, choose, case_id), (False, 240, choose, "false")]
    else:
        branches = [(False, 100, prepare, "source")]
    for upload, y, origin, handle in branches:
        http = http_node(key, 1120, y, timeout, upload)
        status, end = result_nodes("upload" if upload else "run", y, http["id"])
        nodes.extend([http, status, end])
        edges.extend([edge(origin, http, handle), edge(http, status), edge(status, end)])
    return {
        "app": {"name": f"{key} {name}", "description": description, "mode": "workflow", "icon": "📋",
                "icon_background": "#EDF4FF", "use_icon_as_answer_icon": False},
        "kind": "app", "version": "0.3.1", "dependencies": [],
        "workflow": {
            "conversation_variables": [],
            "environment_variables": [
                {"id": stable_id(key + TARGET), "name": TARGET, "selector": ["env", TARGET], "value_type": "string",
                 "description": "企业内网业务 API 基址，不带 /v1", "value": ""},
                {"id": stable_id(key + TOKEN), "name": TOKEN, "selector": ["env", TOKEN], "value_type": "secret",
                 "description": "业务服务授权令牌，在 Dify 中填写", "value": ""},
            ],
            "features": {"file_upload": {"enabled": False}, "opening_statement": "",
                         "retriever_resource": {"enabled": False}, "sensitive_word_avoidance": {"enabled": False},
                         "speech_to_text": {"enabled": False}, "suggested_questions": [],
                         "suggested_questions_after_answer": {"enabled": False}, "text_to_speech": {"enabled": False}},
            "graph": {"viewport": {"x": 0, "y": 40, "zoom": 0.64}, "nodes": nodes, "edges": edges},
        },
    }


def yaml_lines(value: dict | list, depth: int = 0):
    """Emit this DSL's YAML subset without a generator dependency."""
    indentation = "  " * depth
    pairs = ((key + ":", item) for key, item in value.items()) if isinstance(value, dict) else (
        ("-", item) for item in value
    )
    for key, item in pairs:
        if (isinstance(item, dict) and item) or (isinstance(item, list) and any(isinstance(i, (dict, list)) for i in item)):
            yield indentation + key
            yield from yaml_lines(item, depth + 1)
        elif isinstance(item, str) and "\n" in item:
            ending = len(item) - len(item.rstrip("\n"))
            suffix = "-" if not ending else "+" if ending > 1 else ""
            yield f"{indentation}{key} |2{suffix}"
            for line in item.removesuffix("\n").split("\n"):
                yield indentation + "  " + line if line else ""
        else:
            yield indentation + key + " " + json.dumps(item, ensure_ascii=False)


def file_name(key: str) -> str:
    return key + ".yml"


def render(data: dict) -> str:
    return "\n".join(yaml_lines(data)) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check that all committed DSL files match the generator")
    args = parser.parse_args()
    if not args.check:
        OUT.mkdir(parents=True, exist_ok=True)
    for key in WORKFLOWS:
        path = OUT / file_name(key)
        expected = render(definition(key))
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                raise SystemExit(f"{path}: missing or different. Run python scripts/dify_workflows.py")
        else:
            path.write_text(expected, encoding="utf-8", newline="\n")
    print(f"{'Checked' if args.check else 'Generated'} {len(WORKFLOWS)} Dify Workflow YAML files")


if __name__ == "__main__":
    main()
