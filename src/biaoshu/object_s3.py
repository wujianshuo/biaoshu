"""Path-style object storage for an explicitly configured private S3 endpoint."""

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
from urllib.parse import urlsplit

from .config import BusinessError
from .model import PRIVATE_NETWORKS
from .objects import SAFE_DIGEST


_MAX_OBJECT_BYTES = 128 * 1024 * 1024
_MAX_POLICY_BYTES = 65536


def _has_explicit_principals(principal):
    if isinstance(principal, dict):
        if not principal:
            return False
        identities = [identity for value in principal.values() for identity in (value if isinstance(value, list) else [value])]
    else:
        identities = [principal]
    return bool(identities) and all(isinstance(identity, str) and identity.strip()
                                    and not any(char in identity for char in ("*", "?")) for identity in identities)


class S3ObjectFiles:
    def __init__(self, endpoint, bucket, access_key, secret_key, *, region="us-east-1", ca_file=None):
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", parsed.netloc) or parsed.port == 0):
            raise ValueError("BIAOSHU_S3_ENDPOINT 须为内网 S3 API HTTP(S) 根地址。")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]", bucket) or ".." in bucket:
            raise ValueError("BIAOSHU_S3_BUCKET 须填写有效的现有桶名称。")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,128}", access_key) or not re.fullmatch(r"[!-~]{8,256}", secret_key):
            raise ValueError("请配置内网对象存储访问凭据。")
        if not re.fullmatch(r"[a-z0-9-]{1,80}", region):
            raise ValueError("BIAOSHU_S3_REGION 区域格式不正确。")
        if ca_file and parsed.scheme != "https":
            raise ValueError("BIAOSHU_S3_CA_FILE 适用于 HTTPS 连接。")
        self.endpoint = parsed
        self.bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._tls = ssl.create_default_context(cafile=ca_file) if parsed.scheme == "https" else None

    @classmethod
    def from_env(cls):
        return cls(
            os.getenv("BIAOSHU_S3_ENDPOINT", ""), os.getenv("BIAOSHU_S3_BUCKET", ""),
            os.getenv("BIAOSHU_S3_ACCESS_KEY", ""), os.getenv("BIAOSHU_S3_SECRET_KEY", ""),
            region=os.getenv("BIAOSHU_S3_REGION", "us-east-1"), ca_file=os.getenv("BIAOSHU_S3_CA_FILE") or None,
        )

    def _private_address(self, port):
        addresses = {ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(
            self.endpoint.hostname, port, type=socket.SOCK_STREAM,
        )}
        if not addresses or any(not any(ip in network for network in PRIVATE_NETWORKS) for ip in addresses):
            raise BusinessError(503, "对象存储地址必须全部解析到企业内网或本机。")
        return str(min(addresses, key=lambda ip: (ip.version, int(ip))))

    def _signed_headers(self, method, path, body, headers):
        from botocore.auth import S3SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials

        request = AWSRequest(method=method, url=f"{self.endpoint.scheme}://{self.endpoint.netloc}{path}",
                             data=body, headers=headers)
        auth = S3SigV4Auth(Credentials(self._access_key, self._secret_key), "s3", self._region)
        auth.add_auth(request)
        return dict(request.headers.items())

    def _request(self, method, path, *, body=None, authenticate=True, limit=4096):
        port = self.endpoint.port or (443 if self._tls else 80)
        connection = None
        try:
            address = self._private_address(port)
            hostname = self.endpoint.hostname
            if self._tls:
                connection = http.client.HTTPSConnection(hostname, port, timeout=30, context=self._tls)
            else:
                connection = http.client.HTTPConnection(hostname, port, timeout=30)
            # Keep the hostname for HTTP Host and TLS certificate verification, while connecting to the checked IP.
            connection._create_connection = lambda _address, timeout, source: socket.create_connection(
                (address, port), timeout, source,
            )
            headers = {"Host": self.endpoint.netloc}
            if body is not None:
                headers["Content-Type"] = "application/octet-stream"
            if authenticate:
                headers = self._signed_headers(method, path, body, headers)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            read_limit = limit if response.status == 200 else min(limit, 4096)
            result = response.read(read_limit + 1)
            if len(result) > read_limit:
                raise BusinessError(502, "对象存储响应超过大小限制。")
            return response.status, result
        except (OSError, http.client.HTTPException) as exc:
            raise BusinessError(503, "内网对象存储连接失败或请求超时。") from exc
        finally:
            if connection is not None:
                connection.close()

    def probe(self):
        """Require the pre-provisioned bucket and reject anonymous bucket policies."""
        path = "/" + self.bucket
        status, _ = self._request("HEAD", path)
        if status != 200:
            raise BusinessError(503, "配置的私有存储桶不存在或访问被拒绝。")
        status, _ = self._request("HEAD", path, authenticate=False)
        if status not in {403, 404}:
            raise BusinessError(503, "存储桶必须限制匿名访问。")
        status, body = self._request("GET", path + "?policy", limit=_MAX_POLICY_BYTES)
        if status == 404 and re.search(rb"<Code>\s*NoSuchBucketPolicy\s*</Code>", body):
            return
        if status != 200:
            raise BusinessError(503, "无法核查存储桶策略，请检查桶策略读取权限。")
        try:
            policy = json.loads(body)
            statements = policy["Statement"]
            statements = [statements] if isinstance(statements, dict) else statements
            if not isinstance(statements, list) or any(not isinstance(item, dict) for item in statements):
                raise ValueError("Bucket policy statements")
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
            raise BusinessError(503, "存储桶策略格式无效，无法确认访问范围。") from None
        for statement in statements:
            if statement.get("Effect") not in ("Allow", "Deny"):
                raise BusinessError(503, "存储桶策略格式无效，无法确认访问范围。")
            if statement["Effect"] == "Allow" and ("NotPrincipal" in statement
                                                   or not _has_explicit_principals(statement.get("Principal"))):
                raise BusinessError(503, "存储桶策略必须使用明确的访问主体。")

    def _path(self, tenant, project, digest):
        if not isinstance(digest, str) or not SAFE_DIGEST.fullmatch(digest):
            raise BusinessError(400, "文件摘要格式错误。")
        namespace = hashlib.sha256(f"{tenant}\0{project}".encode()).hexdigest()
        return f"/{self.bucket}/{namespace}/{digest[:2]}/{digest}"

    def write(self, tenant, project, data):
        if len(data) > _MAX_OBJECT_BYTES:
            raise BusinessError(413, "文件超过对象存储大小限制。")
        digest = hashlib.sha256(data).hexdigest()
        status, _ = self._request("PUT", self._path(tenant, project, digest), body=data)
        if status not in {200, 201, 204}:
            raise BusinessError(502, "对象存储写入失败，请检查桶权限与服务状态。")
        return digest

    def read(self, tenant, project, digest):
        status, data = self._request("GET", self._path(tenant, project, digest), limit=_MAX_OBJECT_BYTES)
        if status == 404:
            raise BusinessError(404, "文件对象缺失。")
        if status != 200:
            raise BusinessError(502, "对象存储读取失败，请检查桶权限与服务状态。")
        if hashlib.sha256(data).hexdigest() != digest:
            raise BusinessError(409, "文件摘要与档案不一致，请检查存储或从备份恢复。")
        return data
