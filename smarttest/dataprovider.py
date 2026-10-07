"""测试数据字典。

生成器不该凭空编造数据 —— product_id 编一个 "test" 只会得到 404，
把「数据不存在」误判成「服务有 bug」是最典型的假阳性来源。
所以：字段 -> 真实数据 的映射在这里统一管理。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "datasets" / "testdata.json"


class DataProvider:
    def __init__(self, valid: dict[str, list[Any]] | None = None, unknown: dict[str, Any] | None = None):
        self.valid = valid or {}
        self.unknown = unknown or {}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "DataProvider":
        p = Path(path) if path else DEFAULT_DATASET
        if not p.exists():
            return cls()
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls(valid=raw.get("valid", {}), unknown=raw.get("unknown", {}))

    def has(self, field: str) -> bool:
        """该字段是否有可用的真实数据（有则说明它是资源引用，不能随便造值）。"""
        return bool(self.valid.get(field))

    def values(self, field: str) -> list[Any]:
        return list(self.valid.get(field, []))

    def first(self, field: str) -> Any:
        vals = self.valid.get(field)
        return vals[0] if vals else None

    def unknown_value(self, field: str) -> Any:
        return self.unknown.get(field)