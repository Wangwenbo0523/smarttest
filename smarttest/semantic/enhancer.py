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
    retry_attempted: bool = False
    retry_recovered: int = 0

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

        accepted, rejected = self._select(spec, raw_scenarios, [])
        result.scenarios.extend(accepted)

        # 回灌拒绝原因重试一次。实测模型第一次经常不守约束
        # （输出单步场景、编造资源 ID），把「你哪里不合格」明确告诉它，命中率明显更高。
        if rejected and result.provider_name == "llm":
            result.retry_attempted = True
            feedback = [f"{item.scenario_id}: {item.reason}" for item in rejected]
            try:
                retry_raw = self.provider.generate(spec, self.dp, feedback=feedback)
            except LLMUnavailable as exc:
                result.degraded_reason = f"重试失败，保留首轮结果：{exc}"
                retry_raw = []

            if retry_raw:
                accepted_retry, rejected_retry = self._select(
                    spec, retry_raw, result.scenarios
                )
                result.scenarios.extend(accepted_retry)
                result.retry_recovered = len(accepted_retry)
                rejected = rejected_retry

        result.rejected = rejected
        return result

    def _select(
        self, spec: ApiSpec, raw_scenarios: list[dict], accepted: list[ScenarioCase]
    ) -> tuple[list[ScenarioCase], list[RejectedScenario]]:
        """把原始输出筛成可信场景；不合格的连同原因一起返回，不静默丢弃。"""
        picked: list[ScenarioCase] = []
        rejected: list[RejectedScenario] = []
        # 已采纳过的 ID：重试轮里再出现属于正常重复，静默跳过，不该报成「被拒绝」
        seen: set[str] = {s.scenario_id for s in accepted}
        seen_in_batch: set[str] = set()
        # 已采纳过的「场景骨架」：ID 换了但覆盖重叠的同样要拦
        seen_shapes: dict[tuple, str] = {self._shape(s): s.scenario_id for s in accepted}

        for raw in raw_scenarios:
            scenario_id = str(raw.get("scenario_id", "<未命名>"))

            try:
                scenario = ScenarioCase.model_validate(raw)
            except ValidationError as exc:
                first = exc.errors()[0]
                location = ".".join(str(p) for p in first.get("loc", ()))
                rejected.append(
                    RejectedScenario(scenario_id, f"结构校验失败 [{location}] {first.get('msg')}", raw)
                )
                continue

            problem = self._cross_check(spec, scenario)
            if problem:
                rejected.append(RejectedScenario(scenario.scenario_id, problem, raw))
                continue

            if scenario.scenario_id in seen:
                continue
            if scenario.scenario_id in seen_in_batch:
                rejected.append(
                    RejectedScenario(scenario.scenario_id, "同一批输出里场景 ID 重复，已丢弃后出现的", raw)
                )
                continue

            shape = self._shape(scenario)
            if shape in seen_shapes:
                rejected.append(
                    RejectedScenario(
                        scenario.scenario_id,
                        f"与已采纳场景 {seen_shapes[shape]} 步骤骨架一致、覆盖重叠，已丢弃",
                        raw,
                    )
                )
                continue

            seen.add(scenario.scenario_id)
            seen_in_batch.add(scenario.scenario_id)
            seen_shapes[shape] = scenario.scenario_id
            picked.append(scenario)

        return picked, rejected

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

            problem = SemanticEnhancer._align_expected_values(op, step, index)
            if problem:
                return problem

        return None

    @staticmethod
    def _shape(scenario: ScenarioCase) -> tuple:
        """场景骨架：只保留「调了哪个接口、期望什么结果、断言/捕获了哪些字段」。

        实测模型会换个商品 ID、换个场景名把同一个用例再讲一遍，
        ID 去重拦不住这类重复，但执行时间会翻倍、用例数会虚高。
        """
        return tuple(
            (
                step.operation_id,
                step.expect_status,
                tuple(sorted(step.body)) if isinstance(step.body, dict) else (),
                tuple(sorted(step.expect_body)),
                tuple(sorted(step.capture)),
            )
            for step in scenario.steps
        )

    @staticmethod
    def _top_level_field(json_path: str) -> str | None:
        """从 $.a 或 body.a 里取出顶层字段名；嵌套路径返回 None（不猜）。"""
        parts = [p for p in json_path.lstrip("$").lstrip(".").split(".") if p]
        if len(parts) > 1 and parts[0] in ("body", "response", "data"):
            parts = parts[1:]
        return parts[0] if len(parts) == 1 else None

    @staticmethod
    def _align_expected_values(op: Any, step: Any, index: int) -> str | None:
        """把期望值和契约声明的枚举对齐。

        实测模型会写 $.status == 'paid'，而契约声明的是 [CREATED, PAID, CANCELLED]。
        大小写不符属于「可安全纠正」，就地归一；完全对不上则拒绝 ——
        否则它会变成一条指向被测系统的假缺陷，这是最难查的一类假阳性。
        """
        properties = op.response_properties(step.expect_status)
        for json_path, expected in list(step.expect_body.items()):
            field = SemanticEnhancer._top_level_field(json_path)
            if field is None:
                continue
            declared = properties.get(field)
            if not isinstance(declared, dict):
                continue
            enum = declared.get("enum")
            if not isinstance(enum, list) or not enum:
                continue
            if expected in enum:
                continue
            matched = None
            if isinstance(expected, str):
                matched = next(
                    (v for v in enum if isinstance(v, str) and v.lower() == expected.lower()),
                    None,
                )
            if matched is not None:
                step.expect_body[json_path] = matched
                continue
            return f"第 {index} 步期望 {json_path}={expected!r} 不在契约声明的枚举 {enum} 内"

        return None
