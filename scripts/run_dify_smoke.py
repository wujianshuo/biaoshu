"""Run one published workflow on a configured private Dify instance."""

import argparse
import ipaddress
import json
import mimetypes
import os
import socket
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from dify_workflows import WORKFLOWS


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def private_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        raise ValueError("DIFY_BASE_URL 应为私有 Dify HTTP(S) 服务地址")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(item).is_private or ipaddress.ip_address(item).is_unspecified
                            for item in addresses):
        raise ValueError("DIFY_BASE_URL 应解析到内网或环回地址")
    return value if parsed.path.endswith("/v1") else value + "/v1"


def do_json(url: str, token: str, data: bytes, content_type: str, timeout: int) -> dict:
    request = Request(url, data=data, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": content_type,
        "Accept": "application/json",
    })
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Dify HTTP {exc.code}，请检查私有实例、应用发布和应用 API 密钥") from None
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Dify 无法连接或执行超时（{type(exc).__name__}）") from None
    if not isinstance(result, dict):
        raise RuntimeError("Dify 返回的不是 JSON 对象")
    return result


def upload_file(base: str, token: str, user: str, path: Path, timeout: int) -> dict:
    if any(char in path.name for char in '\r\n"'):
        raise ValueError("待测试文件名不能包含引号或换行")
    if not path.is_file():
        raise ValueError("待测试文件不存在")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    boundary = "biaoshu-dify-" + uuid4().hex
    payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="user"\r\n\r\n{user}\r\n'
               f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
               f"Content-Type: {mime}\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    result = do_json(base + "/files/upload", token, payload, "multipart/form-data; boundary=" + boundary, timeout)
    if not isinstance(result.get("id"), str):
        raise RuntimeError("Dify 上传返回缺少文件 ID")
    suffix = path.suffix.lower()
    kind = ("image" if suffix in {".png", ".jpg", ".jpeg"}
            else "custom" if suffix in {".dwg", ".dxf"} else "document")
    return {"transfer_method": "local_file", "upload_file_id": result["id"], "type": kind}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workflow", choices=WORKFLOWS)
    ap.add_argument("--project-id", default="")
    ap.add_argument("--request", type=Path, help="UTF-8 file containing the request_json object")
    ap.add_argument("--upload", type=Path, action="append", default=[], help="repeat for each WF01 file")
    ap.add_argument("--expect-status", help="expected backend status if exact comparison is required")
    ap.add_argument("--timeout", type=int, default=480)
    args = ap.parse_args()
    if (args.upload and args.workflow != "WF01") or (not args.project_id and args.workflow != "WF01"):
        ap.error("只有 WF01 可上传文件或省略 project_id")
    token = os.environ.get(f"DIFY_{args.workflow}_KEY", "")
    if not token:
        ap.error(f"请设置 DIFY_{args.workflow}_KEY")
    try:
        base = private_url(os.environ.get("DIFY_BASE_URL", ""))
        request_json = args.request.read_text(encoding="utf-8") if args.request else "{}"
        if not isinstance(json.loads(request_json), dict):
            raise ValueError("--request 内容必须是 JSON 对象")
        user = "biaoshu-smoke-" + uuid4().hex
        inputs = {"project_id": args.project_id, "request_json": request_json}
        if args.upload:
            inputs["files"] = [upload_file(base, token, user, path, args.timeout) for path in args.upload]
        request = json.dumps({"inputs": inputs, "response_mode": "blocking", "user": user}).encode()
        result = do_json(base + "/workflows/run", token, request, "application/json", args.timeout)
        run = result.get("data") or {}
        outputs = run.get("outputs") or {}
        if run.get("status") != "succeeded":
            raise RuntimeError(f"Dify 运行失败，执行 ID {result.get('workflow_run_id')}，请查看 Dify 执行日志")
        status = outputs.get("status")
        http_status = outputs.get("http_status")
        if not isinstance(http_status, (int, float)) or not 200 <= http_status < 300 or status in ("backend_error", "invalid_response", None):
            raise RuntimeError(f"业务流程失败，HTTP {http_status}，状态 {status}，执行 ID {result.get('workflow_run_id')}")
        if args.expect_status and status != args.expect_status:
            raise RuntimeError(f"业务状态为 {status}，预期 {args.expect_status}")
        body = json.loads(outputs["result_json"])
        print(json.dumps({"workflow": args.workflow, "run_id": result.get("workflow_run_id"), "status": status,
                          "http_status": http_status, "project_id": body.get("project_id") or args.project_id}, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
