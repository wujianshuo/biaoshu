import hashlib
import os
import re
import tempfile
from pathlib import Path

from .config import BusinessError


SAFE_DIGEST = re.compile(r"[a-f0-9]{64}")


class ObjectFiles:
    def __init__(self, destination: Path):
        self.destination = destination

    def path(self, tenant: str, project: str, digest: str) -> Path:
        if not SAFE_DIGEST.fullmatch(digest):
            raise BusinessError(400, "文件摘要格式错误")
        namespace = hashlib.sha256(f"{tenant}\0{project}".encode()).hexdigest()
        return self.destination / namespace / digest[:2] / digest

    def write(self, tenant: str, project: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        dst = self.path(tenant, project, digest)
        if dst.is_file():
            return digest
        dst.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=dst.parent, delete=False) as tmp:
            tmp.write(data)
            path = Path(tmp.name)
        try:
            os.replace(path, dst)
        finally:
            path.unlink(missing_ok=True)
        return digest

    def read_path(self, tenant: str, project: str, digest: str) -> Path:
        path = self.path(tenant, project, digest)
        if not path.is_file():
            raise BusinessError(404, "文件对象缺失")
        return path

    def read(self, tenant: str, project: str, digest: str) -> bytes:
        data = self.read_path(tenant, project, digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise BusinessError(409, "文件摘要与档案不一致，请检查存储或从备份恢复")
        return data
