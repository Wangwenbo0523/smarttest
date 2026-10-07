"""Spec Parser：OpenAPI 3.x -> IR。

刻意做成确定性实现（不用 LLM）：文档解析是有唯一正确答案的事情，
交给模型只会引入不确定性。
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from .ir import ApiSpec, Operation, Parameter

_HTTP_METHODS = ("get", "post", "put", "patch", "delete")
_MAX_REF_DEPTH = 12


class OpenApiParser:
    def __init__(self, document: dict[str, Any]):
        self.doc = document

    # ---------- 构造入口 ----------

    @classmethod
    def from_file(cls, path: str | Path) -> "OpenApiParser":
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        doc = yaml.safe_load(text) if p.suffix in (".yaml", ".yml") else json.loads(text)
        return cls(doc)

    @classmethod
    def from_url(cls, url: str, timeout: int = 10) -> "OpenApiParser":
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return cls(json.loads(resp.read().decode("utf-8")))

    # ---------- $ref 解析 ----------

    def _resolve(self, node: Any) -> Any:
        depth = 0
        while isinstance(node, dict) and "$ref" in node:
            if depth > _MAX_REF_DEPTH:
                raise ValueError("$ref 嵌套过深，可能存在循环引用")
            ref = node["$ref"]
            if not ref.startswith("#/"):
                raise ValueError(f"当前仅支持文档内部 $ref，收到: {ref}")
            cursor: Any = self.doc
            for part in ref[2:].split("/"):
                cursor = cursor[part]
            node = cursor
            depth += 1
        return node

    # ---------- 主流程 ----------

    def parse(self) -> ApiSpec:
        info = self.doc.get("info", {})
        spec = ApiSpec(title=info.get("title", "unnamed"), version=str(info.get("version", "0")))

        for path, raw_path_item in self.doc.get("paths", {}).items():
            path_item = self._resolve(raw_path_item)
            shared_params = list(path_item.get("parameters", []))

            for method in _HTTP_METHODS:
                raw_op = path_item.get(method)
                if not raw_op:
                    continue
                raw_op = self._resolve(raw_op)
                spec.operations.append(
                    self._build_operation(raw_op, method, path, shared_params)
                )
        return spec

    def _build_operation(
        self,
        raw_op: dict[str, Any],
        method: str,
        path: str,
        shared_params: list[Any],
    ) -> Operation:
        tags = raw_op.get("tags") or ["default"]
        op = Operation(
            operation_id=raw_op.get("operationId")
            or f"{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}",
            method=method.upper(),
            path=path,
            tag=str(tags[0]),
            summary=raw_op.get("summary", ""),
            responses=raw_op.get("responses", {}),
        )

        for raw_param in list(shared_params) + list(raw_op.get("parameters", [])):
            param = self._resolve(raw_param)
            op.parameters.append(
                Parameter(
                    name=param["name"],
                    location=param.get("in", "query"),
                    required=bool(param.get("required", False)),
                    schema=self._resolve(param.get("schema", {})),
                )
            )

        for code, raw_response in (raw_op.get("responses") or {}).items():
            if not str(code).isdigit():
                continue
            response = self._resolve(raw_response)
            content = response.get("content", {})
            media = content.get("application/json") or next(iter(content.values()), None)
            if media:
                op.response_schemas[int(code)] = self._resolve(media.get("schema", {}))

        raw_body = raw_op.get("requestBody")
        if raw_body:
            body = self._resolve(raw_body)
            op.body_required = bool(body.get("required", False))
            content = body.get("content", {})
            media = content.get("application/json") or next(iter(content.values()), None)
            if media:
                op.body_schema = self._resolve(media.get("schema", {}))

        return op