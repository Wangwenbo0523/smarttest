"""本地环境变量加载。

命令行入口和演示服务都要从 `.env.local` 读模型密钥，逻辑只有一处，
放在这里避免两边各写一份。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = ".env.local"


def load_local_env(root: str | Path) -> None:
    """把 `<root>/.env.local` 里的键值写进 `os.environ`。

    不引入 python-dotenv：十几行的事，没必要多一个依赖。
    用 `setdefault` 而不是覆盖，保证命令行显式传入的环境变量优先级更高。
    `.env.*` 已在 .gitignore 中，密钥不会进仓库。
    """
    env_file = Path(root) / ENV_FILE
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())
