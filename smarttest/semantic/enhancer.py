"""语义增强编排：生成 -> 校验 -> 交叉核对 -> 只放行可信的场景。

这里是「模型只做加法、不做减法」这条原则真正的落点：
模型提出的场景，必须先过两道关卡才能进测试套件。

  第一道：结构校验（Pydantic）
      步数、字段类型、必填项。结构不对直接拒。

  第二道：契约交叉核对
      operationId 是否真实存在？期望状态码是否在契约里声明过？
      路径参数是否给全？—— 这一步专治模型幻觉。
      编造出来的接口在这一关必然被拦下。

被拒的场景会带着原因记录下来，而不是静默丢弃 ——
静默丢弃等于把「模型在胡说」这件事藏起来。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..dataprovider import DataProvider
from ..ir import ApiSpec
from .models import ScenarioCase
from .providers import LLMUnavailable, ScenarioProvider


@dataclass
class RejectedScenario:
    scenario_id: str
    reason: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EnhancementResult:
    scenarios: list[ScenarioCase] = field(default_factory=list)
    rejected: list[RejectedScenario] = field(default_factory=list)
    provider_name: str = "rule-based"
    degraded_reason: str = ""

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


class SemanticEnhancer:
    def __init__(self, provider: ScenarioProvider, dp: DataProvider | None = None):
        self.provider = provider
        self.dp = dp or DataProvider()

    def enhance(self, spec: ApiSpec) -> EnhancementResult:
        result = EnhancementResult(provider_name=self.provider.name)

        try:
            raw_scenarios = self.provider.generate(spec, self.dp)
        except LLMUnavailable as exc:
            # 降级而不是失败：模型不可用时规则推导接手，覆盖率不塌成 0
            from .providers import RuleBasedScenarioProvider

            result.provider_name = "rule-based"
            result.degraded_reason = str(exc)
            raw_scenarios = RuleBasedScenarioProvider().generate(spec, self.dp)

        seen: set[str] = set()
        for raw in raw_scenarios:
            scenario_id = str(raw.get("scenario_id", "<未命名>"))

            try:
                scenario = ScenarioCase.model_validate(raw)
            except ValidationError as exc:
                first = exc.errors()[0]
                location = ".".join(str(p) for p in first.get("loc", ()))
                result.rejected.append(
                    RejectedScenario(scenario_id, f"结构校验失败 [{location}] {first.get('msg')}", raw)
                )
                continue

            problem = self._cross_check(spec, scenario)
            if problem:
                result.rejected.append(RejectedScenario(scenario.scenario_id, problem, raw))
                continue

            if scenario.scenario_id in seen:
                result.rejected.append(
                    RejectedScenario(scenario.scenario_id, "场景 ID 重复，已丢弃后出现的同名场景", raw)
                )
                continue

            seen.add(scenario.scenario_id)
            result.scenarios.append(scenario)

        return result

    # ---------- 契约交叉核对 ----------

    @staticmethod
    def _cross_check(spec: ApiSpec, scenario: ScenarioCase) -> str | None:
        for index, step in enumerate(scenario.steps, 1):
            op = spec.find(step.operation_id)
            if op is None:
                return f"第 {index} 步引用了契约中不存在的 operationId: {step.operation_id}"

            documented = sorted(int(c) for c in op.responses if str(c).isdigit())
            if step.expect_status not in documented:
                return (
                    f"第 {index} 步期望状态码 {step.expect_status} 未在契约中声明"
                    f"（{op.operation_id} 已声明 {documented}）"
                )

            missing = [p.name for p in op.path_params() if p.name not in step.path_params]
            if missing:
                return f"第 {index} 步缺少必需的路径参数: {missing}"

        return None