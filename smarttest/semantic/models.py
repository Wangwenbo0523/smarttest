"""场景模型：模型输出必须是「数据」，不能是「代码」。

这是整个语义增强层最重要的一条设计约束。

让 LLM 直接生成 pytest 代码，风险有三个：语法可能错、风格会漂移、
最要命的是它可能编造出契约里根本不存在的接口。

所以这里把模型的能力限制在一个**封闭的表达空间**内：
它只能描述「调用哪个 operationId、传什么参数、期望什么状态码」，
URL 由解释器从契约里查出来，断言由确定性代码执行。
模型编不出不存在的接口 —— 因为 operationId 会在校验期被逐个核对。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_STEPS = 8


class ScenarioStep(BaseModel):
    """场景中的一步。只引用 operationId，不出现任何 URL。"""

    operation_id: str = Field(..., description="必须引用契约中已存在的 operationId")
    path_params: dict[str, str] = Field(default_factory=dict, description="路径参数，可用 {{变量}} 引用前面捕获的值")
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = Field(default=None, description="请求体，可用 {{变量}} 引用前面捕获的值")
    capture: dict[str, str] = Field(
        default_factory=dict, description="变量名 -> 响应字段的 JSONPath，如 order_id -> $.order_id"
    )
    expect_body: dict[str, str] = Field(
        default_factory=dict, description="JSONPath -> 期望值，可用 {{变量}} 引用前面捕获的值"
    )
    expect_status: int = Field(..., description="期望状态码，必须是契约中声明过的")

    @field_validator("capture")
    @classmethod
    def _normalize_capture(cls, value: dict[str, str]) -> dict[str, str]:
        """capture 的语义是「变量名 -> JSONPath」，但模型经常写反。

        实测 DeepSeek 输出了 {"$.order_id": "order_id"}，结果变量名变成了
        "$.order_id"，后面 {{order_id}} 永远解析不出来，整条场景报废。
        这种方向性错误可以安全纠正：JSONPath 一定以 $ 开头。
        """
        normalized: dict[str, str] = {}
        for key, target in (value or {}).items():
            key, target = str(key), str(target)
            if key.startswith("$") and not target.startswith("$"):
                normalized[target] = key
            else:
                normalized[key] = target
        return normalized

    @field_validator("operation_id")
    @classmethod
    def _operation_id_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("operation_id 不能为空")
        return value.strip()


class ScenarioCase(BaseModel):
    """一个跨接口的业务场景。"""

    scenario_id: str = Field(..., description="场景唯一标识，蛇形命名")
    title: str = Field(..., description="一句话说明这个场景在验证什么")
    rationale: str = Field(..., description="设计依据，如 idempotency:same_key")
    source: str = Field(default="llm", description="rule 或 llm")
    steps: list[ScenarioStep] = Field(..., description="按顺序执行，2-8 步")

    @field_validator("rationale")
    @classmethod
    def _rationale_machine_readable(cls, value: str) -> str:
        """rationale 会被归因模块拿去匹配缺陷模式库，必须是「类别:子类」格式。

        实测模型会写成中文长句，那样归因只能落到「契约不符」兜底分支，
        报告里也会显示一大段说明当标题。这里做一次归一，保证下游可用。
        """
        value = (value or "").strip()
        if ":" in value and " " not in value.split(":", 1)[0]:
            return value
        return "scenario:llm_generated"

    @field_validator("steps")
    @classmethod
    def _step_count_in_range(cls, value: list[ScenarioStep]) -> list[ScenarioStep]:
        if len(value) < 2:
            raise ValueError("场景至少需要 2 步；单步验证属于契约层用例，不应放在场景层")
        if len(value) > MAX_STEPS:
            raise ValueError(f"场景步数不能超过 {MAX_STEPS} 步，请拆分")
        return value


# 给模型看的结构约束（用于 response_format 与提示词）
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["scenarios"],
    "properties": {
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["scenario_id", "title", "rationale", "steps"],
                "properties": {
                    "scenario_id": {"type": "string"},
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": MAX_STEPS,
                        "items": {
                            "type": "object",
                            "required": ["operation_id", "expect_status"],
                            "properties": {
                                "operation_id": {"type": "string"},
                                "path_params": {"type": "object"},
                                "headers": {"type": "object"},
                                "body": {},
                                "capture": {"type": "object"},
                                "expect_body": {"type": "object"},
                                "expect_status": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        }
    },
}