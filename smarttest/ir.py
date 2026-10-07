"""中间模型（IR）。

所有上游规范（OpenAPI / Postman Collection / 自研文档）先转成 IR，
下游的用例设计、脚本生成只依赖 IR —— 这样换规范格式只需要加一个 adapter。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Parameter:
    name: str
    location: str  # path | query | header
    required: bool
    schema: dict[str, Any]


@dataclass
class Operation:
    operation_id: str
    method: str
    path: str
    tag: str
    summary: str
    parameters: list[Parameter] = field(default_factory=list)
    body_schema: dict[str, Any] | None = None
    body_required: bool = False
    responses: dict[str, Any] = field(default_factory=dict)
    response_schemas: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def success_status(self) -> int:
        for code in sorted(self.responses):
            if str(code).isdigit() and 200 <= int(code) < 300:
                return int(code)
        return 200

    @property
    def documented_errors(self) -> list[int]:
        return sorted(int(c) for c in self.responses if str(c).isdigit() and int(c) >= 400)

    def response_properties(self, status: int | None = None) -> dict[str, Any]:
        """取响应体的属性定义（$ref 已在解析期展开）。"""
        code = status if status is not None else self.success_status
        schema = self.response_schemas.get(code, {})
        return schema.get("properties", {})

    def path_params(self) -> list[Parameter]:
        return [p for p in self.parameters if p.location == "path"]

    def header_params(self) -> list[Parameter]:
        return [p for p in self.parameters if p.location == "header"]

    @property
    def signature(self) -> str:
        return f"{self.method} {self.path}"


@dataclass
class ApiSpec:
    title: str
    version: str
    operations: list[Operation] = field(default_factory=list)

    def find(self, operation_id: str) -> Operation | None:
        for op in self.operations:
            if op.operation_id == operation_id:
                return op
        return None