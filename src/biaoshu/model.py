import http.client
import ipaddress
import json
import math
import os
import re
import socket
from urllib.parse import urlsplit

from .config import BusinessError


PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7",
)))


class LocalModel:
    """Call a configured OpenAI-compatible model on a loopback or private network."""

    def __init__(self):
        self.base = os.getenv("BIAOSHU_MODEL_BASE_URL", "").rstrip("/")
        self.name = os.getenv("BIAOSHU_MODEL_NAME", "").strip()
        self.api_key = os.getenv("BIAOSHU_MODEL_API_KEY", "")
        self.embedding_name = os.getenv("BIAOSHU_EMBEDDING_MODEL", "").strip()
        self.timeout = int(os.getenv("BIAOSHU_MODEL_TIMEOUT", "90"))
        if not 1 <= self.timeout <= 600:
            raise ValueError("模型超时必须为 1 至 600 秒")
        self._endpoint = self._parse_endpoint() if self.base else None

    def _parse_endpoint(self):
        endpoint = urlsplit(self.base)
        if (endpoint.scheme not in ("http", "https") or not endpoint.hostname
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", endpoint.path)):
            raise ValueError("模型地址应为企业内网 HTTP(S) 接口根路径，例如 http://ollama:11434/v1")
        return endpoint

    @property
    def configured(self):
        return bool(self._endpoint and self.name)

    def _addresses(self):
        endpoint = self._endpoint
        port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
        results = socket.getaddrinfo(endpoint.hostname, port, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(row[4][0]) for row in results]
        if not addresses or any(not any(ip in network for network in PRIVATE_NETWORKS) for ip in addresses):
            raise BusinessError(503, "模型地址必须解析到企业内网或本机地址")
        return port, addresses

    def _post(self, path: str, data: dict) -> dict:
        if self._endpoint is None:
            raise BusinessError(503, "尚未配置 BIAOSHU_MODEL_BASE_URL")
        endpoint = self._endpoint
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        connection = None
        try:
            port, addresses = self._addresses()
            connection_type = http.client.HTTPSConnection if endpoint.scheme == "https" else http.client.HTTPConnection
            connection = connection_type(endpoint.hostname, port, timeout=self.timeout)
            # Connect to the checked address, preserving the configured Host and TLS hostname.
            checked_address = str(addresses[0])
            connection._create_connection = lambda address, timeout, source: socket.create_connection(
                (checked_address, address[1]), timeout, source,
            )
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = "Bearer " + self.api_key
            connection.request("POST", endpoint.path + path, payload, headers)
            response = connection.getresponse()
            if response.status != 200:
                raise BusinessError(502, f"本地模型 HTTP {response.status}")
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise BusinessError(502, "本地模型响应过大")
            return json.loads(raw)
        except (OSError, http.client.HTTPException) as exc:
            raise BusinessError(502, "本地模型连接失败或调用超时") from exc
        except (ValueError, TypeError) as exc:
            raise BusinessError(502, "本地模型未返回标准 JSON") from exc
        finally:
            if connection is not None:
                connection.close()

    def complete(self, prompt: str, data: dict, max_tokens: int = 3000) -> dict:
        if not self.configured:
            raise BusinessError(503, "尚未配置本地模型，请设置 BIAOSHU_MODEL_BASE_URL 和 BIAOSHU_MODEL_NAME")
        envelope = self._post("/chat/completions", {
            "model": self.name, "temperature": 0.1, "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
        })
        try:
            choice = envelope["choices"][0]
            if choice.get("finish_reason") == "length":
                raise BusinessError(502, "本地模型输出达到长度上限")
            body = choice["message"]["content"].strip()
            body = re.sub(r"^\s*<think>.*?</think>\s*", "", body, count=1, flags=re.S)
            fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, re.S | re.I)
            result = json.loads(fenced.group(1) if fenced else body)
            if not isinstance(result, dict):
                raise ValueError("not an object")
            return result
        except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            raise BusinessError(502, "本地模型没有返回可解析的 JSON 对象") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.embedding_name:
            raise BusinessError(503, "尚未配置 BIAOSHU_EMBEDDING_MODEL")
        envelope = self._post("/embeddings", {"model": self.embedding_name, "input": texts})
        try:
            entries = sorted(envelope["data"], key=lambda row: row["index"])
            if [row["index"] for row in entries] != list(range(len(texts))):
                raise ValueError("invalid embedding indexes")
            vectors = [row["embedding"] for row in entries]
            if any(not isinstance(values, list) or not 1 <= len(values) <= 4096
                   or any(type(value) not in (float, int) or not math.isfinite(value) for value in values)
                   for values in vectors):
                raise ValueError("invalid embedding")
            return vectors
        except (TypeError, KeyError, ValueError, OverflowError) as exc:
            raise BusinessError(502, "本地嵌入模型返回结构无效") from exc
