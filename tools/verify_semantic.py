# -*- coding: utf-8 -*-
"""验证语义增强层的 LLM 通路与幻觉拦截。

用一个假的 OpenAI 兼容服务，喂进 4 个场景：
  1 个合法            -> 必须被采纳
  1 个编造接口        -> 必须被拦下
  1 个编造状态码      -> 必须被拦下
  1 个结构不合法      -> 必须被拦下
再验证模型不可用时会降级到规则推导，而不是整体失败。

用法：python tools/verify_semantic.py
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from smarttest.dataprovider import DataProvider  # noqa: E402
from smarttest.parser import OpenApiParser  # noqa: E402
from smarttest.semantic import (  # noqa: E402
    OpenAICompatibleProvider,
    SemanticEnhancer,
)
from smarttest.semantic.providers import RuleBasedScenarioProvider  # noqa: E402

CONTRACT = ROOT / "target_service" / "contract" / "openapi.yaml"

VALID_PAYLOAD = {"product_id": "P001", "quantity": 1}

CANNED = {
    "scenarios": [
        {
            "scenario_id": "llm_idempotent_create",
            "title": "相同幂等键重复下单只应创建一笔订单",
            "rationale": "idempotency:same_key",
            "steps": [
                {
                    "operation_id": "createOrder",
                    "headers": {"Idempotency-Key": "k-1"},
                    "body": VALID_PAYLOAD,
                    "expect_status": 201,
                    "capture": {"order_id": "$.order_id"},
                },
                {
                    "operation_id": "createOrder",
                    "headers": {"Idempotency-Key": "k-1"},
                    "body": VALID_PAYLOAD,
                    "expect_status": 201,
                    "expect_body": {"$.order_id": "{{order_id}}"},
                },
            ],
        },
        {
            "scenario_id": "llm_hallucinated_operation",
            "title": "编造一个契约里不存在的退款接口",
            "rationale": "refund:full",
            "steps": [
                {"operation_id": "refundOrder", "expect_status": 200},
                {"operation_id": "createOrder", "body": VALID_PAYLOAD, "expect_status": 201},
            ],
        },
        {
            "scenario_id": "llm_undocumented_status",
            "title": "编造一个契约里没声明的状态码",
            "rationale": "teapot:bonus",
            "steps": [
                {"operation_id": "createOrder", "body": VALID_PAYLOAD, "expect_status": 418},
                {
                    "operation_id": "getOrder",
                    "path_params": {"order_id": "{{order_id}}"},
                    "expect_status": 200,
                },
            ],
        },
        {
            "scenario_id": "llm_single_step",
            "title": "只有一步，不构成场景",
            "rationale": "broken:structure",
            "steps": [{"operation_id": "createOrder", "expect_status": 201}],
        },
    ]
}


class _FakeLLM(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps(
            {"choices": [{"message": {"content": json.dumps(CANNED, ensure_ascii=False)}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        pass


def start_fake_llm() -> tuple[HTTPServer, int]:
    server = HTTPServer(("127.0.0.1", 0), _FakeLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main() -> int:
    spec = OpenApiParser.from_file(CONTRACT).parse()
    dp = DataProvider.load()
    failures: list[str] = []

    # ---------- 1. LLM 通路 ----------
    server, port = start_fake_llm()
    try:
        provider = OpenAICompatibleProvider(
            base_url=f"http://127.0.0.1:{port}/v1", api_key="test-key", model="fake-model"
        )
        result = SemanticEnhancer(provider, dp).enhance(spec)

        print(f"[1] LLM 通路：采纳 {len(result.scenarios)} 个，拒绝 {result.rejected_count} 个")
        for rejected in result.rejected:
            print(f"    - 拒绝 {rejected.scenario_id}: {rejected.reason}")

        if len(result.scenarios) != 1 or result.scenarios[0].scenario_id != "llm_idempotent_create":
            failures.append("合法场景没有被正确采纳")
        if len(result.rejected) != 3:
            failures.append(f"应当拒绝 3 个场景，实际拒绝 {len(result.rejected)} 个")

        reasons = {r.scenario_id: r.reason for r in result.rejected}
        if "不存在" not in reasons.get("llm_hallucinated_operation", ""):
            failures.append("编造的 operationId 没有被拦截")
        if "未在契约中声明" not in reasons.get("llm_undocumented_status", ""):
            failures.append("编造的状态码没有被拦截")
        if "结构校验失败" not in reasons.get("llm_single_step", ""):
            failures.append("结构不合法的场景没有被拦截")
    finally:
        server.shutdown()

    # ---------- 2. 模型不可用时降级 ----------
    dead = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:1/v1", api_key="test-key", model="fake", timeout=2
    )
    degraded = SemanticEnhancer(dead, dp).enhance(spec)
    print(f"[2] 模型不可用：提供方={degraded.provider_name}，采纳 {len(degraded.scenarios)} 个")
    print(f"    降级原因: {degraded.degraded_reason[:80]}")
    if degraded.provider_name != "rule-based" or not degraded.scenarios:
        failures.append("模型不可用时没有正确降级到规则推导")

    # ---------- 3. 规则推导本身 ----------
    ruled = RuleBasedScenarioProvider().generate(spec, dp)
    print(f"[3] 规则推导：产出 {len(ruled)} 个场景 -> {[s['scenario_id'] for s in ruled]}")
    if len(ruled) != 2:
        failures.append(f"规则推导应产出 2 个场景，实际 {len(ruled)} 个")

    print()
    if failures:
        for item in failures:
            print(f"[FAIL] {item}")
        return 1
    print("[PASS] LLM 通路、幻觉拦截、降级机制全部符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())