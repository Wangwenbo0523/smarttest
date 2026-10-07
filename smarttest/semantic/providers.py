"""场景提供方：规则推导 与 大模型增强，两条通路产出同一种结构。

分开的意义在于：没有模型的环境里流水线照样闭环（规则推导），
配了模型就自动升级（语义增强），下游的校验、渲染、执行完全不用改。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol

from ..dataprovider import DataProvider
from ..ir import ApiSpec, Operation
from ..rules import build_valid_payload
from .models import RESPONSE_SCHEMA

# 「状态冲突」类状态码的优先级：重复操作被拒绝时应命中的码
CONFLICT_PREFERENCE = (409, 410, 412, 423)

SYSTEM_PROMPT = """你是一位资深测试开发工程师，负责为接口设计跨接口的业务场景测试。

严格约束：
1. 只能使用给定清单里已存在的 operationId，绝不能编造接口。
2. expect_status 必须是该接口在契约中声明过的状态码。
3. 只描述「调用哪个接口、传什么参数、期望什么结果」，不要写代码、不要写 URL。
4. 场景必须跨 2-8 步，能体现状态依赖、幂等、权限、一致性等业务语义。
5. 不要重复契约层已经覆盖的单接口参数校验用例。

只输出 JSON，结构为 {"scenarios": [...]}。"""


class ScenarioProvider(Protocol):
    name: str

    def generate(self, spec: ApiSpec, dp: DataProvider) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------
# 规则推导
# --------------------------------------------------------------------------
class RuleBasedScenarioProvider:
    """从 IR 的 HTTP 语义推导场景，不依赖任何模型。

    之所以做得这么「笨」，是因为它必须是确定性兜底：
    模型不可用、超时、输出不合法时，覆盖率不能塌成 0。
    """

    name = "rule-based"

    def generate(self, spec: ApiSpec, dp: DataProvider) -> list[dict[str, Any]]:
        scenarios: list[dict[str, Any]] = []

        creators = [
            op for op in spec.operations
            if op.method == "POST" and "{" not in op.path and op.body_schema
        ]

        for creator in creators:
            collection = creator.path
            payload = build_valid_payload(creator, dp)

            for action in self._resource_actions(spec, collection, creator):
                scenario = self._lifecycle_scenario(creator, action, payload)
                if scenario:
                    scenarios.append(scenario)

            detail = self._detail_reader(spec, collection, creator)
            if detail:
                scenario = self._round_trip_scenario(creator, detail, payload)
                if scenario:
                    scenarios.append(scenario)

        return scenarios

    # ---------- 派生规则 ----------

    @staticmethod
    def _resource_actions(spec: ApiSpec, collection: str, creator: Operation) -> list[Operation]:
        """资源级动作：POST /orders/{id}/pay —— 路径在集合之下且带路径参数。"""
        return [
            op for op in spec.operations
            if op.method == "POST"
            and op is not creator
            and op.path.startswith(collection + "/")
            and "{" in op.path
        ]

    @staticmethod
    def _detail_reader(spec: ApiSpec, collection: str, creator: Operation) -> Operation | None:
        """详情读取：GET /orders/{id}，且只有一个路径参数（不是子资源）。"""
        for op in spec.operations:
            if (
                op.method == "GET"
                and op.path.startswith(collection + "/")
                and op.path.count("{") == 1
            ):
                return op
        return None

    @staticmethod
    def _id_param(op: Operation) -> str | None:
        params = op.path_params()
        return params[0].name if params else None

    def _lifecycle_scenario(self, creator: Operation, action: Operation, payload: dict) -> dict | None:
        id_name = self._id_param(action)
        if not id_name:
            return None
        # 防幻觉：只有当创建接口的响应里真的有这个字段时才生成捕获
        if id_name not in creator.response_properties():
            return None

        # 重复执行应被拒绝的状态码，从契约里推导。
        # 注意不能取「最小的 4xx」：资源上一步刚创建成功，404 在这里没有意义，
        # 真正表达「状态冲突」的是 409 这类码。所以按语义优先级挑。
        conflict = next(
            (code for code in CONFLICT_PREFERENCE if code in action.documented_errors), None
        )
        if conflict is None:
            return None

        ref = "{{" + id_name + "}}"
        return {
            "scenario_id": f"lifecycle_{creator.operation_id}_{action.operation_id}",
            "title": f"{creator.summary or creator.operation_id} 后执行 {action.summary or action.operation_id}，"
                     f"并验证重复操作被拒绝",
            "rationale": f"state:lifecycle:{action.operation_id}",
            "source": "rule",
            "steps": [
                {
                    "operation_id": creator.operation_id,
                    "body": payload,
                    "expect_status": creator.success_status,
                    "capture": {id_name: f"$.{id_name}"},
                },
                {
                    "operation_id": action.operation_id,
                    "path_params": {id_name: ref},
                    "expect_status": action.success_status,
                },
                {
                    "operation_id": action.operation_id,
                    "path_params": {id_name: ref},
                    "expect_status": conflict,
                },
            ],
        }

    def _round_trip_scenario(self, creator: Operation, detail: Operation, payload: dict) -> dict | None:
        id_name = self._id_param(detail)
        if not id_name or id_name not in creator.response_properties():
            return None

        return {
            "scenario_id": f"round_trip_{creator.operation_id}_{detail.operation_id}",
            "title": f"{creator.summary or creator.operation_id} 后查询详情，验证数据一致",
            "rationale": f"consistency:round_trip:{detail.operation_id}",
            "source": "rule",
            "steps": [
                {
                    "operation_id": creator.operation_id,
                    "body": payload,
                    "expect_status": creator.success_status,
                    "capture": {id_name: f"$.{id_name}"},
                },
                {
                    "operation_id": detail.operation_id,
                    "path_params": {id_name: "{{" + id_name + "}}"},
                    "expect_status": detail.success_status,
                    "expect_body": {f"$.{id_name}": "{{" + id_name + "}}"},
                },
            ],
        }


# --------------------------------------------------------------------------
# 大模型增强
# --------------------------------------------------------------------------
class LLMUnavailable(RuntimeError):
    pass


class OpenAICompatibleProvider:
    """OpenAI 兼容接口的模型提供方。

    只负责「发请求、取 JSON」这一件事；输出是否合法由 enhancer 校验，
    这里不做任何信任假设。
    """

    name = "llm"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def generate(self, spec: ApiSpec, dp: DataProvider) -> list[dict[str, Any]]:
        context = build_context(spec)
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "以下是接口契约清单（只允许使用其中的 operationId）：\n"
                        + json.dumps(context, ensure_ascii=False, indent=2)
                        + "\n\n输出 JSON Schema 约束：\n"
                        + json.dumps(RESPONSE_SCHEMA, ensure_ascii=False)
                    ),
                },
            ],
        }

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMUnavailable(f"模型接口返回 {exc.code}: {exc.read()[:200]!r}") from exc
        except Exception as exc:  # 网络问题一律降级到规则推导
            raise LLMUnavailable(f"无法访问模型接口: {exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except Exception as exc:
            raise LLMUnavailable(f"模型响应无法解析为 JSON: {exc}") from exc

        scenarios = parsed.get("scenarios")
        if not isinstance(scenarios, list):
            raise LLMUnavailable("模型响应缺少 scenarios 数组")
        return scenarios


def build_context(spec: ApiSpec) -> list[dict[str, Any]]:
    """把契约压缩成模型能读懂的清单。只给语义信息，不给完整 schema。"""
    context: list[dict[str, Any]] = []
    for op in spec.operations:
        props = (op.body_schema or {}).get("properties", {})
        context.append(
            {
                "operationId": op.operation_id,
                "method": op.method,
                "summary": op.summary,
                "path_params": [p.name for p in op.path_params()],
                "header_params": [p.name for p in op.header_params()],
                "body_fields": {
                    name: sub.get("description", sub.get("type", "")) for name, sub in props.items()
                },
                "documented_statuses": sorted(
                    int(c) for c in op.responses if str(c).isdigit()
                ),
            }
        )
    return context


def build_provider(verbose: bool = True) -> ScenarioProvider:
    """按环境变量选择提供方，模型不可用时自动降级。"""
    api_key = os.environ.get("SMARTTEST_LLM_API_KEY", "").strip()
    if not api_key:
        if verbose:
            print("       [i] 未配置 SMARTTEST_LLM_API_KEY，使用规则推导（不影响流水线闭环）")
        return RuleBasedScenarioProvider()

    return OpenAICompatibleProvider(
        base_url=os.environ.get("SMARTTEST_LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=api_key,
        model=os.environ.get("SMARTTEST_LLM_MODEL", "gpt-4o-mini"),
    )