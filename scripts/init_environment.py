"""Create deployment configuration without echoing credentials."""

import argparse
import json
import os
import re
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="生成第一阶段部署用的随机 PostgreSQL 密码和独立令牌。")
    parser.add_argument("--tenant", default="construction")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.tenant):
        parser.error("--tenant 应为 1 至 64 个字母、数字或下划线及连字符")
    root = Path(__file__).resolve().parents[1]
    template = root.joinpath(".env.example").read_text(encoding="utf-8")
    accounts = [{"tenant": args.tenant, "actor": role, "role": role,
                 "projects": ["*"], "token": secrets.token_urlsafe(32)}
                for role in ("editor", "reviewer")]
    text = template.replace("BIAOSHU_POSTGRES_PASSWORD=\n", f"BIAOSHU_POSTGRES_PASSWORD={secrets.token_hex(24)}\n")
    text = text.replace("BIAOSHU_TOKENS_JSON='[]'", "BIAOSHU_TOKENS_JSON='" + json.dumps(accounts) + "'")
    target = root / ".env"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error("项目已有 .env，请检查文件并保留已配置的凭证")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print("已生成 .env。请核对文件权限，使用对应角色的令牌进行试跑。")


if __name__ == "__main__":
    main()
