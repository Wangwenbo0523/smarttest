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

# 下面这几条约束不是凭空写的，每一条都对应一次真实模型跑出来的问题：
#   约束 3 ← 模型编造了 prod-1001 这类不存在的商品 ID，导致 404 被误判成缺陷
#   约束 4 ← 模型用了 ${order_id}，而解释器认的是 {{order_id}}，变量根本替换不上
#   约束 5 ← 模型输出了 5 个单步场景，全部被结构校验拒掉
#   约束 6 ← rationale 写成中文长句，归因模块没法拿它匹配缺陷模式库
SYSTEM_PROMPT = """你是一位资深测试开发工程师，负责为接口设计跨接口的业务场景测试。

严格约束：
1. 只能使用给定清单里已存在的 operationId，绝不能编造接口。
2. expect_status 必须是该接口在契约中声明过的状态码。
3. 请求参数中的资源 ID 必须取自 available_test_data，绝不能自己编造。
4. 变量引用统一写成 {{变量名}} 双花括号；不要用 ${} 或其它写法。
5. 每个场景必须 2-8 步。单接口的参数校验由契约层负责，你只做跨接口场景。
6. rationale 必须使用「类别:子类」的机器可读格式，例如
   idempotency:same_key、state:lifecycle、consistency:round_trip、permission:cross_tenant。
7. capture 的方向是「变量名 -> JSONPath」，例如 {"order_id": "$.order_id"}，
   千万不要写反。expect_body 同理，键是 $.字段名 形式的 JSONPath；
   两者都不要加 body.、data. 这类前缀。
8. 只描述「调用哪个接口、传什么参数、期望什么结果」，不要写代码、不要写 URL。

只输出 JSON，结构为 {"scenarios": [...]}。"""


class ScenarioProvider(Protocol):
    name: str

    def generate(
        self, spec: ApiSpec, dp: DataProvider, feedback: list[str] | None = None
    ) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------
# 规则推导
# --------------------------------------------------------------------------
class RuleBasedScenarioProvider:
    """从 IR 的 HTTP 语义推导场景，不依赖任何模型。

    之所以做得这么「笨」，是因为它必须是确定性兜底：
    模型不可用、超时、输出不合法时，覆盖率不能塌成 0。
    """

    name = "rule-based"

    def generate(
        self, spec: ApiSpec, dp: DataProvider, feedback: list[str] | None = None
    ) -> list[dict[str, Any]]:
        # 规则推导不需要反馈：它本来就是确定性的，重跑一次结果一样
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

    def generate(
        self, spec: ApiSpec, dp: DataProvider, feedback: list[str] | None = None
    ) -> list[dict[str, Any]]:
        context = build_context(spec, dp)

        prompt_parts = [
            "以下是接口契约清单（只允许使用其中的 operationId）：\n",
            json.dumps(context, ensure_ascii=False, indent=2),
            "\n\n输出 JSON Schema 约束：\n",
            json.dumps(RESPONSE_SCHEMA, ensure_ascii=False),
        ]

        if feedback:
            # 把「上一次哪里不合格」明确回灌，比单纯重试一次有效得多
            prompt_parts.append(
                "\n\n上一次的输出有以下问题，请修正后重新输出完整结果：\n"
                + "\n".join(f"- {item}" for item in feedback)
            )

        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "".join(prompt_parts)},
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


def build_context(spec: ApiSpec, dp: DataProvider | None = None) -> dict[str, Any]:
    """把契约压缩成模型能读懂的清单。只给语义信息，不给完整 schema。"""
    operations: list[dict[str, Any]] = []
    for op in spec.operations:
        props = (op.body_schema or {}).get("properties", {})
        operations.append(
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

    return {
        "operations": operations,
        # 真实可用数据必须显式给出，否则模型一定会自己编 ——
        # 实测它编了 prod-1001，结果 3 个场景全挂在 404 上，被误判成缺陷。
        "available_test_data": (dp.valid if dp else {}),
        "rules": [
            "资源 ID 只能取 available_test_data 里的值",
            "变量引用写成 {{变量名}}",
            "每个场景 2-8 步",
            "rationale 用 类别:子类 格式",
        ],
    }


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