"""Start the local backend on Windows or Linux without Docker."""

import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / ".env"
    if not path.is_file():
        raise SystemExit("请先执行 python scripts/init_environment.py 并填写模型配置。")
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"BIAOSHU_[A-Z0-9_]+", key):
            raise SystemExit(f".env 第 {number} 行应为 BIAOSHU_ 开头的 KEY=VALUE")
        if len(value) > 1 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    database = (ROOT / "data" / "local.sqlite3").as_posix()
    os.environ.setdefault("BIAOSHU_DATABASE_URL", f"sqlite:///{database}")
    os.environ.setdefault("BIAOSHU_FILE_ROOT", str(ROOT / "data" / "objects"))
    sys.path.insert(0, str(ROOT / "src"))
    import uvicorn

    uvicorn.run("biaoshu.app:create_app", factory=True, host="127.0.0.1",
                port=int(os.getenv("BIAOSHU_PORT", "8091")), proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
