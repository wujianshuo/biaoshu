import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


class BusinessError(Exception):
    def __init__(self, status: int, detail):
        self.status = status
        self.detail = detail
        super().__init__(str(detail))


@dataclass(frozen=True)
class Actor:
    tenant: str
    name: str
    role: str
    projects: tuple[str, ...]

    def allows(self, project_id: str) -> bool:
        return "*" in self.projects or project_id in self.projects

    def require(self, *roles: str):
        if self.role not in roles:
            raise BusinessError(403, "当前角色无权操作")


@dataclass(frozen=True)
class Settings:
    dsn: str
    files: Path
    accounts: dict[str, Actor] = field(repr=False)
    upload_limit: int = 25 * 1024 ** 2

    @classmethod
    def from_env(cls):
        dsn = os.getenv("BIAOSHU_DATABASE_URL", "").strip()
        if not dsn:
            raise ValueError("请设置 BIAOSHU_DATABASE_URL")
        destination = Path(os.getenv("BIAOSHU_FILE_ROOT", "./data/objects")).resolve()
        accounts = {}
        for entry in json.loads(os.getenv("BIAOSHU_TOKENS_JSON", "[]")):
            token = entry["token"]
            role = entry["role"]
            values = entry["projects"]
            if not isinstance(token, str) or len(token) < 24 or not token.isascii() or any(c.isspace() for c in token):
                raise ValueError("Bearer token 应为至少 24 位无空格 ASCII 文本")
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError("projects 必须是项目 ID 数组")
            tenant, actor = entry["tenant"], entry["actor"]
            if not all(isinstance(v, str) and 0 < len(v) <= 96 and v.strip() == v for v in (tenant, actor)):
                raise ValueError("tenant 和 actor 必须为有效标识")
            if role not in {"editor", "reviewer", "admin", "viewer"} or token in accounts:
                raise ValueError("无效角色或重复 token")
            accounts[token] = Actor(tenant, actor, role, tuple(values))
        if not accounts:
            raise ValueError("请设置 BIAOSHU_TOKENS_JSON，服务不会创建默认账户")
        max_mb = int(os.getenv("BIAOSHU_UPLOAD_MB", "25"))
        if not 1 <= max_mb <= 100:
            raise ValueError("BIAOSHU_UPLOAD_MB 应为 1 至 100")
        return cls(dsn, destination, accounts, max_mb * 1024 ** 2)

    def authenticate(self, authorization: str | None) -> Actor:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() == "bearer" and token and token.isascii():
            for candidate, actor in self.accounts.items():
                if hmac.compare_digest(candidate, token):
                    return actor
        raise BusinessError(401, "需要有效的 Bearer token")
