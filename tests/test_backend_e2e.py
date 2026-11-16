import hashlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import uvicorn

from biaoshu.app import create_app
from biaoshu.config import Actor, Settings
from biaoshu.model import LocalModel


EDITOR = "test-editor-only-4a84cb44"
REVIEWER = "test-review-only-504674bf"
OUTSIDER = "test-other-tenant-cb3014"
TITLE = "模拟道路提升工程"
TENDER = """项目名称：模拟道路提升工程。
合同总工期为120日历天。
投标人须持有有效的施工资质。
施工方案应有现场检查和验收安排。
技术评审：进度措施满足要求得8分。
施工部署与主要施工方案
进度、质量与安全保证措施
"""
AMENDMENT = "对招标文件工期条款作修改，合同总工期为112日历天。"


def mock_completion(data):
    if "snippets" not in data:
        fact = data["facts"].get("duration", {}).get("value", "")
        return {
            "content": (f"合同总工期为{fact}。落实责任分工。按本项目要求组织现场检查，"
                        "材料进场核验、工序交接、分项验收均形成记录。"),
            "responded_requirement_ids": [row["id"] for row in data["requirements"]],
            "responded_score_ids": [row["id"] for row in data["scores"]],
            "reference_ids": [item["reference_id"] for item in data["experience"]],
            "evidence_ids": [item["evidence_id"] for item in data["enterprise_evidence"]],
            "confirmed_fact_keys": ["duration"],
            "pending_items": [],
        }
    items = []
    for row in data["snippets"]:
        for term, shaped in [
            ("项目名称：模拟道路提升工程。", {"kind": "fact", "fact_key": "project_name", "value": TITLE}),
            ("合同总工期为120日历天。", {"kind": "fact", "fact_key": "duration", "value": "120日历天"}),
            ("合同总工期为112日历天。", {"kind": "fact", "fact_key": "duration", "value": "112日历天"}),
            ("投标人须持有有效的施工资质。", {"kind": "requirement", "category": "B", "mandatory": True,
                                         "evidence_type": "qualification"}),
            ("施工方案应有现场检查和验收安排。", {"kind": "requirement", "category": "C", "mandatory": True,
                                           "chapter_hint": "施工部署与主要施工方案"}),
            ("技术评审：进度措施满足要求得8分。", {"kind": "score", "points": 8, "chapter_hint": "进度、质量与安全保证措施"}),
            ("施工部署与主要施工方案", {"kind": "chapter"}),
            ("进度、质量与安全保证措施", {"kind": "chapter"}),
        ]:
            if term in row["text"]:
                items.append({**shaped, "source_id": row["id"], "quote": term, "title": term})
    return {"items": items}


class MockModelHTTP(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        obj = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = json.loads(obj["messages"][-1]["content"])
        result = mock_completion(data)
        body = json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(result, ensure_ascii=False),
        }}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class BackendE2E(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.model_http = ThreadingHTTPServer(("127.0.0.1", 0), MockModelHTTP)
        self.addCleanup(self.model_http.server_close)
        model_thread = threading.Thread(target=self.model_http.serve_forever, daemon=True)
        model_thread.start()
        self.addCleanup(self.model_http.shutdown)
        with patch.dict(os.environ, {
            "BIAOSHU_MODEL_BASE_URL": f"http://127.0.0.1:{self.model_http.server_port}/v1",
            "BIAOSHU_MODEL_NAME": "synthetic-local-model",
        }):
            model = LocalModel()
        root = Path(self.temp.name)
        dsn = os.getenv("BIAOSHU_E2E_POSTGRES_DSN") or f"sqlite:///{root / 'test.sqlite'}"
        tenant = "smoke-" + uuid4().hex
        settings = Settings(dsn, root / "files", {
            EDITOR: Actor(tenant, "operator", "editor", ("*",)),
            REVIEWER: Actor(tenant, "reviewer", "reviewer", ("*",)),
            OUTSIDER: Actor("outsider", "viewer", "viewer", ("*",)),
        })
        self.web_socket = socket.socket()
        self.addCleanup(self.web_socket.close)
        self.web_socket.bind(("127.0.0.1", 0))
        self.host = f"http://127.0.0.1:{self.web_socket.getsockname()[1]}"
        app = create_app(settings, model)
        uvicorn_config = uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=3)
        self.server = uvicorn.Server(uvicorn_config)
        server_thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.web_socket]}, daemon=True)
        server_thread.start()

        def halt():
            self.server.should_exit = True
            server_thread.join(timeout=8)

        self.addCleanup(halt)
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(.05)
        self.assertTrue(self.server.started, "backend failed to start")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path, payload=None, *, expect=200, token=EDITOR, raw=False, method=None, content_type="application/json"):
        data = (payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
                if payload is not None else None)
        headers = {"Content-Type": content_type}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(self.host + path, data, headers, method=method)
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as http_error:
            response = http_error
        with response as opened:
            self.assertEqual(opened.status, expect, (path, opened.read().decode(errors="replace") if opened.status != expect else ""))
            body = opened.read()
        return body if raw else json.loads(body)

    def multipart(self):
        boundary = "bid-test-" + uuid4().hex
        parts = []
        for field, value in (("project_id", ""), ("request_json", json.dumps({
            "project_name": TITLE, "document_kinds": {"tender.txt": "tender", "amendment.txt": "amendment"},
        }, ensure_ascii=False))):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
        for filename, content in (("tender.txt", TENDER), ("amendment.txt", AMENDMENT)):
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                          f'Content-Type: text/plain\r\n\r\n{content}\r\n').encode())
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def seed_library(self):
        knowledge = {
            "category": "KB03", "title": "施工部署与进度、质量与安全保证措施", "source": "虚构施工技术库",
            "source_version": "demo-1", "sha256": hashlib.sha256(b"demo-1").hexdigest(),
            "chunks": [{"content": "施工部署与主要施工方案。进度、质量与安全保证措施。", "locator": {"section": "1"}}],
        }
        item = self.call("/v1/knowledge/procedure/versions", knowledge, expect=201)
        self.call(f'/v1/knowledge/procedure/versions/{item["version"]}/review',
                  {"decision": "approve"}, token=REVIEWER)
        cert = {
            "evidence_type": "qualification", "title": "模拟施工资质证书", "source": "虚构证照来源",
            "sha256": hashlib.sha256(b"fake-cert").hexdigest(), "subject_status": "active",
            "valid_from": "2020-01-01", "valid_until": "2099-01-01", "details": {},
        }
        item = self.call("/v1/evidence/cert/versions", cert, expect=201)
        self.call(f'/v1/evidence/cert/versions/{item["version"]}/review', {"decision": "approve"}, token=REVIEWER)
        return knowledge

    def test_knowledge_original_is_hashed_and_requires_approval(self):
        boundary = "knowledge-fixture"
        raw = "模拟工序标准，先检查设备状态再组织工序交接。"
        data = json.dumps({"title": "模拟工序标准", "source": "模拟来源", "source_version": "v1", "category": "KB03"},
                          ensure_ascii=False)
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="metadata_json"\r\n\r\n{data}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="procedure.txt"\r\n'
                f'Content-Type: text/plain\r\n\r\n{raw}\r\n--{boundary}--\r\n').encode()
        result = self.call("/v1/knowledge/raw-demo/ingest", body,
                           content_type=f"multipart/form-data; boundary={boundary}", expect=201)
        self.assertEqual(result["sha256"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertTrue(result["metadata"]["source_sha256_verified"])
        prefix = f'/v1/knowledge/raw-demo/versions/{result["version"]}'
        self.assertEqual(self.call(prefix + "/original", raw=True), raw.encode())
        query = {"query": "设备状态"}
        self.assertEqual(self.call("/v1/knowledge/search", query)["results"], [])
        self.call(prefix + "/review", {"decision": "approve"}, token=REVIEWER)
        self.assertEqual(self.call("/v1/knowledge/search", query)["results"][0]["sha256"], result["sha256"])

    def test_complete_business_flow(self):
        self.assertIn(b"assets/app.js", self.call("/", raw=True, token=None))
        self.assertEqual(self.call("/healthz", token=None)["status"], "ready")
        self.call("/v1/projects", token=None, expect=401)
        knowledge = self.seed_library()
        request_body, content_type = self.multipart()
        start = self.call("/v1/workflows/WF01/upload", request_body, content_type=content_type)
        self.assertEqual(start["document_count"], 2)
        project_id = start["project_id"]
        project = f"/v1/projects/{project_id}"
        self.call(project, token=OUTSIDER, expect=404)

        def run(code, expect=200, **values):
            return self.call(f"/v1/workflows/{code}/run", {"project_id": project_id, **values}, expect=expect)

        def read(kind, key="latest"):
            return self.call(f"{project}/records/{kind}/{key}")

        def confirm(kind, row, methods=None, *, expect=200, token=REVIEWER):
            return self.call(f'{project}/records/{kind}/{row["key"]}/review', {
                "revision": row["revision"], "decision": "approve", "note": "人工复核测试", "resolutions": methods or {},
            }, expect=expect, token=token)

        run("WF02", expect=409)
        confirm("archive", read("archive"), expect=403, token=EDITOR)
        confirm("archive", read("archive"))
        run("WF02")
        analysis = read("analysis")
        source = next(item for item in self.call(project + "/analysis-sources")["snippets"]
                      if "施工方案应有现场检查和验收安排。" in item["text"])
        revision = analysis["revision"]
        correction = {
            "revision": revision, "note": "复核后修订条款分类描述",
            "items": [{"kind": "requirement", "category": "C", "source_id": source["id"], "mandatory": True,
                       "quote": "施工方案应有现场检查和验收安排。", "title": "检查和验收",
                       "chapter_hint": "施工部署与主要施工方案"}],
        }
        self.call(project + "/analysis", {**correction, "items": [
            {**correction["items"][0], "quote": "文件中不存在的伪造条款"},
        ]}, method="PUT", expect=422)
        self.call(project + "/analysis", correction, method="PUT")
        analysis = read("analysis")
        self.assertGreater(analysis["revision"], revision)
        confirm("analysis", analysis, {"amendments_reviewed": True}, expect=409)
        facts = [item for item in analysis["data"]["items"] if item.get("fact_key") == "duration"]
        original = next(row for row in facts if row["value"] == "120日历天")
        amended = next(row for row in facts if row["value"] == "112日历天")
        confirm("analysis", analysis, {"amendments_reviewed": True, "supersessions": [
            {"old_id": original["id"], "new_id": amended["id"]},
        ]})
        self.assertEqual(run("WF03")["rows"], 2)
        self.assertEqual(run("WF04")["rows"], 1)
        self.assertEqual(read("score")["data"]["rows"][0]["points"], 8)
        run("WF05")
        match = read("match")
        self.assertEqual(len(match["data"]["rows"]), 1)
        candidate = match["data"]["rows"][0]
        self.assertEqual(candidate["candidate_keys"], ["cert@1"])
        confirm("match", match, {"evidence_decisions": {
            candidate["item_id"]: {"status": "accepted", "keys": ["cert@1"]},
        }})
        self.assertEqual(len(self.call(project + "/technical-plan")["chapters"]), 2)
        output = run("WF06")
        self.assertEqual(len(output["chapters"]), 2)
        self.assertGreater(run("WF09")["blocking_count"], 0)
        for row in output["chapters"]:
            chapter = read("chapter", row["chapter_id"])
            self.assertIn("112日历天", chapter["data"]["content"])
            self.assertNotIn("120日历天", chapter["data"]["content"])
            self.assertIn("amendment.txt", [item["file_name"] for item in chapter["data"]["source_documents"]])
            self.assertTrue(chapter["data"]["enterprise_references"])
            confirm("chapter", chapter)
        self.assertEqual(run("WF09")["blocking_count"], 0)
        confirm("audit", read("audit"))
        self.assertEqual(run("WF10")["status"], "draft_ready")
        run("WF10", mode="reviewed", expect=409)
        confirm("artifact", read("artifact", "technical-draft"))
        self.assertEqual(run("WF10", mode="reviewed")["status"], "reviewed_ready")
        prefix = f"{project}/artifacts/"
        draft = self.call(prefix + "technical-draft/download", raw=True)
        self.assertEqual(draft, self.call(prefix + "technical-reviewed/download", raw=True))
        with zipfile.ZipFile(io.BytesIO(draft)) as docx:
            self.assertIn("112日历天", docx.read("word/document.xml").decode("utf8"))
        for file in ("requirements", "scores", "evidence-matches", "review-report"):
            with zipfile.ZipFile(io.BytesIO(self.call(prefix + file + "/download", raw=True))) as xlsx:
                self.assertIn("xl/worksheets/sheet1.xml", xlsx.namelist())
        self.assertEqual(len(self.call(project + "/records/run")["records"]) >= 8, True)
        pending = self.call("/v1/knowledge/procedure/versions", {
            **knowledge, "source_version": "demo-2", "sha256": hashlib.sha256(b"demo-2").hexdigest(),
        }, expect=201)
        self.call(f'/v1/knowledge/procedure/versions/{pending["version"]}/review',
                  {"decision": "approve"}, token=REVIEWER)
        self.call(prefix + "technical-reviewed/download", expect=409)
        self.assertGreater(run("WF09")["blocking_count"], 0)


if __name__ == "__main__":
    unittest.main()
