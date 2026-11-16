"""Parse checked-in Dify DSL independently and exercise its inputs and HTTP routes."""

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from textwrap import indent
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from dify_workflows import OUT, TARGET, TOKEN, WORKFLOWS, definition, file_name


EXAMPLES = {
    "WF01": {"project_name": "示例工程", "industry": "房建",
             "document_kinds": {"addendum.txt": "amendment"}},
    "WF02": {"document_ids": ["document-123"]},
    "WF03": {},
    "WF04": {},
    "WF05": {"as_of": "2026-11-16"},
    "WF06": {"chapter_ids": ["chapter-123"]},
    "WF09": {},
    "WF10": {"mode": "draft"},
}
PROJECT_ID = "11111111-1111-4111-8111-111111111111"
TEST_TOKEN = "local-fake-token"
SELECTOR = re.compile(r"\{\{#([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)#\}\}")


def load_yaml() -> tuple[dict, str]:
    content = {name: (OUT / file_name(name)).read_text(encoding="utf-8") for name in WORKFLOWS}
    try:
        import yaml
    except ImportError:
        if not shutil.which("docker"):
            raise SystemExit("DSL validation requires PyYAML or Docker Compose CLI (daemon not needed)") from None
        wrapped = "services: {}\n" + "".join(f"x-{name.lower()}:\n{indent(data, '  ')}" for name, data in content.items())
        process = subprocess.run([
            "docker", "compose", "--project-name", "biaoshu-dsl-check", "-f", "-", "config", "--no-interpolate",
            "--format", "json",
        ], input=wrapped, text=True, encoding="utf-8", capture_output=True, check=True)
        data = json.loads(process.stdout)
        return {name: data["x-" + name.lower()] for name in content}, "Docker Compose YAML parser"
    return {name: yaml.safe_load(data) for name, data in content.items()}, "PyYAML"


def string_selectors(data) -> Iterator[tuple[str, str]]:
    if isinstance(data, dict):
        for value in data.values():
            yield from string_selectors(value)
    elif isinstance(data, list):
        for value in data:
            yield from string_selectors(value)
    elif isinstance(data, str):
        yield from SELECTOR.findall(data)


def check_graph(data: dict) -> dict:
    graph = data["workflow"]["graph"]
    nodes = {node["id"]: node["data"] for node in graph["nodes"]}
    assert len(nodes) == len(graph["nodes"])
    assert data["kind"] == "app" and data["app"]["mode"] == "workflow" and data["version"] == "0.3.1"
    assert nodes["start"]["type"] == "start"
    assert data["dependencies"] == []
    for raw_node in graph["nodes"]:
        assert isinstance(raw_node["data"]["title"], str)
        assert all(type(raw_node["position"][axis]) is int for axis in ("x", "y"))
    declared = {"env": {item["name"] for item in data["workflow"]["environment_variables"]}}
    assert declared["env"] == {TARGET, TOKEN}
    assert all(item["value"] == "" for item in data["workflow"]["environment_variables"])
    for node_id, node in nodes.items():
        if node["type"] == "start":
            declared[node_id] = {item["variable"] for item in node["variables"]}
        if node["type"] == "code":
            declared[node_id] = set(node["outputs"])
            compile(node["code"], node_id, "exec")
        if node["type"] == "http-request":
            declared[node_id] = {"body", "status_code", "headers", "files"}
            assert node["method"] == "post" and not node["retry_config"]["retry_enabled"]
            assert node["authorization"]["config"]["api_key"] == f"{{{{#env.{TOKEN}#}}}}"
    for node_id, node in nodes.items():
        bindings = list(string_selectors(node))
        if node["type"] == "code":
            bindings.extend(item["value_selector"] for item in node["variables"])
        if node["type"] == "end":
            assert {item["variable"] for item in node["outputs"]} == {"status", "result_json", "http_status"}
            bindings.extend(item["value_selector"] for item in node["outputs"])
        if node["type"] == "http-request":
            bindings.extend(part["file"] for part in node["body"]["data"] if part["type"] == "file")
        if node["type"] == "if-else":
            bindings.extend(item["variable_selector"] for case in node["cases"] for item in case["conditions"])
        assert all(field in declared[owner] for owner, field in bindings), node_id
    edges = graph["edges"]
    assert len({edge["id"] for edge in edges}) == len(edges)
    for edge in edges:
        assert edge["data"]["sourceType"] == nodes[edge["source"]]["type"]
        assert edge["data"]["targetType"] == nodes[edge["target"]]["type"]
    end_nodes = {node_id for node_id, node in nodes.items() if node["type"] == "end"}
    outgoing = {node_id: [edge for edge in edges if edge["source"] == node_id] for node_id in nodes}
    stack = ["start"]
    seen = set()
    while stack:
        item = stack.pop()
        assert item not in seen, ("unexpected branch merge or cycle", item)
        seen.add(item)
        if not outgoing[item]:
            assert item in end_nodes
        if nodes[item]["type"] == "if-else":
            assert {edge["sourceHandle"] for edge in outgoing[item]} == {
                "false", *[case["case_id"] for case in nodes[item]["cases"]],
            }
        stack.extend(edge["target"] for edge in outgoing[item])
    assert set(nodes) == seen
    return nodes


def code(node: dict, **kwargs) -> dict:
    namespace: dict = {}
    exec(compile(node["code"], node["title"], "exec"), namespace)
    return namespace["main"](**kwargs)


def interpolate(value: str, inputs: dict) -> str:
    return SELECTOR.sub(lambda m: inputs[m[1]][m[2]], value)


class LocalEndpoint(BaseHTTPRequestHandler):
    received: list[tuple[str, str, dict, list]] = []

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        content_type = self.headers["Content-Type"]
        files = []
        if content_type.startswith("multipart/form-data"):
            message = BytesParser(policy=default).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body)
            form = {}
            for part in message.iter_parts():
                key = part.get_param("name", header="content-disposition")
                filename = part.get_filename()
                if filename:
                    files.append((key, filename, part.get_payload(decode=True)))
                else:
                    form[key] = part.get_payload(decode=True).decode()
            payload = {**json.loads(form["request_json"]), "project_id": form["project_id"]}
        else:
            payload = json.loads(body)
        token = self.headers.get("Authorization", "")
        self.received.append((self.path, token, payload, files))
        status = 200 if token == f"Bearer {TEST_TOKEN}" else 401
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        response = {"status": "pending_review", "project_id": payload["project_id"]} if status == 200 else {"detail": "Unauthorized"}
        self.wfile.write(json.dumps(response).encode())


def http_call(node: dict, prepared: dict, token: str, files: list[tuple[str, bytes]]) -> tuple[int, str]:
    values = {"prepare": prepared, "env": {TOKEN: token}}
    url = interpolate(node["url"], values)
    auth = interpolate(node["authorization"]["config"]["api_key"], values)
    body = node["body"]
    if body["type"] == "json":
        content = interpolate(body["data"][0]["value"], values).encode()
        content_type = "application/json"
    else:
        boundary = "boundary-" + uuid4().hex
        parts = []
        for part in body["data"]:
            key = part["key"]
            if part["type"] == "text":
                content_part = interpolate(part["value"], values).encode()
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
                             + content_part + b"\r\n")
            else:
                for name, file in files:
                    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{name}"\r\n'
                                 "Content-Type: application/octet-stream\r\n\r\n".encode() + file + b"\r\n")
        content = b"".join(parts) + f"--{boundary}--\r\n".encode()
        content_type = f"multipart/form-data; boundary={boundary}"
    request = Request(url, data=content, method="POST", headers={
        "Authorization": "Bearer " + auth, "Content-Type": content_type,
    })
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except HTTPError as exc:
        return exc.code, exc.read().decode()


def verify_run(key: str, nodes: dict, base: str) -> None:
    project_id = "" if key == "WF01" else PROJECT_ID
    prepared = code(nodes["prepare"], project_id=project_id, request_json=json.dumps(EXAMPLES[key]), api_base=base + "/")
    status, body = http_call(nodes["http_run"], prepared, TEST_TOKEN, [])
    path, auth, payload, files = LocalEndpoint.received[-1]
    assert path == f"/v1/workflows/{key}/run" and auth == f"Bearer {TEST_TOKEN}"
    assert payload == {**EXAMPLES[key], "project_id": project_id or None} and not files
    assert code(nodes["status_run"], body=body, http_status=status) == {"status": "pending_review"}
    status, body = http_call(nodes["http_run"], prepared, "wrong-test-token", [])
    assert status == 401 and json.loads(body)["detail"] == "Unauthorized"
    assert code(nodes["status_run"], body=body, http_status=status) == {"status": "backend_error"}
    assert code(nodes["status_run"], body="bad gateway", http_status=502) == {"status": "backend_error"}
    assert code(nodes["status_run"], body="not JSON", http_status=200) == {"status": "invalid_response"}
    for invalid in ["[]", "{oops}", '{"project_id":"replaced"}', '{"foo":NaN}', '{"key":1,"key":2}']:
        try:
            code(nodes["prepare"], project_id=project_id, request_json=invalid, api_base=base)
        except ValueError:
            pass
        else:
            raise AssertionError((key, invalid))
    if key == "WF01":
        status, body = http_call(nodes["http_upload"], prepared, TEST_TOKEN,
                                 [("tender.txt", b"a"), ("addendum.txt", b"b")])
        path, auth, payload, files = LocalEndpoint.received[-1]
        assert path == "/v1/workflows/WF01/upload" and auth == f"Bearer {TEST_TOKEN}"
        assert payload == {**EXAMPLES[key], "project_id": ""}
        assert files == [("files", "tender.txt", b"a"), ("files", "addendum.txt", b"b")]
        assert code(nodes["status_upload"], body=body, http_status=status) == {"status": "pending_review"}


def main() -> None:
    flows, parser = load_yaml()
    with ThreadingHTTPServer(("127.0.0.1", 0), LocalEndpoint) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for name, flow in flows.items():
                assert flow == definition(name), f"{name}: committed DSL is out of sync with the generator"
                nodes = check_graph(flow)
                verify_run(name, nodes, f"http://127.0.0.1:{server.server_port}")
                print(f"{name} PASS")
        finally:
            server.shutdown()
            worker.join()
    print(f"Validated {len(flows)} DSLs with {parser}. Local HTTP contract simulation only, Dify not started.")


if __name__ == "__main__":
    main()
