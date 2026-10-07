"""失败归因：把「测试失败」翻译成「这到底是谁的问题」。

测试失败 != 发现缺陷。环境挂了、数据没了、用例自己写错了，
在 pytest 眼里都是 failed。不把这几类拆开，误报率就降不下来，
报告也就没人敢信 —— 这是测试工具能不能落地的分水岭。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from .runner import CaseResult, RunSummary

REAL_DEFECT = "真实缺陷"
ENV_ISSUE = "环境问题"
DATA_ISSUE = "数据问题"
CASE_ISSUE = "用例问题"
CONTRACT_DRIFT = "契约变更"
NEEDS_REVIEW = "待人工确认"

SEVERITY = {
    REAL_DEFECT: "high",
    CONTRACT_DRIFT: "medium",
    DATA_ISSUE: "medium",
    ENV_ISSUE: "low",
    CASE_ISSUE: "low",
    NEEDS_REVIEW: "medium",
}

_MARKER = re.compile(
    r"\[kind=(?P<kind>\w+)\]\[case_id=(?P<case_id>[^\]]+)\]\[basis=(?P<basis>[^\]]+)\]"
    r"(?:\[expected=(?P<expected>\d+)\])?(?:\[actual=(?P<actual>\d+)\])?"
)

# 「我们给的前置数据不存在」和「服务有 bug」是两回事，
# 混在一起报就会把假阳性算成缺陷 —— 实测 LLM 编造商品 ID 时踩过这个坑。
_MISSING_DATA_KEYWORDS = ("不存在", "not found", "no such", "未找到")

_ENV_KEYWORDS = (
    "ConnectError", "ConnectTimeout", "ReadTimeout", "Connection refused",
    "Max retries exceeded", "Name or service not known", "Temporary failure in name resolution",
)

# 缺陷模式库：设计依据 -> (人话标题, 修复建议)
_PATTERN_LIBRARY: list[tuple[str, str, str]] = [
    ("boundary:minimum", "数值下界未校验", "入参未拦截下界-1 的取值，建议补充 minimum 校验"),
    ("boundary:maximum", "数值上界未校验", "入参未拦截上界+1 的取值，建议补充 maximum 校验并评估防刷"),
    ("boundary:minLength", "字符串长度下界未校验", "建议补充 minLength 校验"),
    ("boundary:maxLength", "字符串长度上界未校验", "建议补充 maxLength 校验，防止超长入参落库"),
    ("type:", "入参类型未校验", "类型不符时应返回 422，建议补充类型校验"),
    ("required:", "必填校验缺失", "缺少必填参数时应返回 422，建议补充必填校验"),
    ("enum:", "枚举取值未校验", "非法枚举值应被拦截，建议把字段改为枚举类型"),
    ("resource:not_found", "资源不存在时行为不符", "建议核对 404 语义与错误码规范"),
    ("idempotency:", "幂等语义未实现", "相同 Idempotency-Key 重复提交产生了多笔订单，建议引入幂等表或唯一索引"),
    ("money:", "金额精度错误", "金额使用了 float 运算，建议改用 Decimal 并 quantize 到分"),
    ("state:", "状态机流转错误", "状态流转未按契约拦截，建议补充状态前置校验"),
    ("nullable:", "可空字段处理错误", "建议核对可空字段的判空逻辑"),
    ("baseline:", "基准用例失败", "最基础的合法请求都失败，优先排查环境或接口不可用"),
]

# 粗粒度根因：同一字段的「上界+1」和「下界-1」属于同一个缺陷，必须归并
_COARSE_MAP: list[tuple[str, str]] = [
    ("boundary:minimum", "range"),
    ("boundary:maximum", "range"),
    ("boundary:minLength", "length"),
    ("boundary:maxLength", "length"),
    ("type:", "type"),
    ("required:", "required"),
    ("enum:", "enum"),
    ("resource:not_found", "resource"),
    ("nullable:", "nullable"),
    ("idempotency:", "idempotency"),
    ("money:", "money"),
    ("state:", "state"),
    ("baseline:", "baseline"),
]


def _lookup_pattern(basis: str) -> tuple[str, str]:
    for prefix, title, suggestion in _PATTERN_LIBRARY:
        if basis.startswith(prefix):
            return title, suggestion
    return "契约不符", "请人工确认是服务实现问题，还是契约本身需要更新"


# 归并后的条目按「粗粒度根因」统一措辞：
# minimum-1 和 maximum+1 合成一条时，单独说「下界未校验」就不准确了。
_COARSE_SUMMARY: dict[str, tuple[str, str]] = {
    "range": ("数值范围未校验", "入参未拦截契约声明的上下界，建议补齐 minimum / maximum 校验并评估防刷"),
    "length": ("字符串长度未校验", "入参未拦截契约声明的长度上下界，建议补齐 minLength / maxLength 校验"),
    "type": ("入参类型未校验", "类型不符时应返回 422，建议补充类型校验"),
    "required": ("必填校验缺失", "缺少必填参数时应返回 422，建议补充必填校验"),
    "enum": ("枚举取值未校验", "非法枚举值应被拦截，建议把字段改为枚举类型"),
    "resource": ("资源不存在时行为不符", "建议核对 404 语义与错误码规范"),
    "nullable": ("可空字段处理错误", "建议核对可空字段的判空逻辑"),
    "idempotency": ("幂等语义未实现", "相同幂等键重复提交产生了多笔订单，建议引入幂等表或唯一索引"),
    "money": ("金额精度错误", "金额使用了 float 运算，建议改用 Decimal 并 quantize 到分"),
    "state": ("状态机流转错误", "状态流转未按契约拦截，建议补充状态前置校验"),
    "baseline": ("基准用例失败", "最基础的合法请求都失败，优先排查环境或接口不可用"),
}


def _coarse(basis: str) -> str:
    for prefix, coarse in _COARSE_MAP:
        if basis.startswith(prefix):
            return coarse
    return basis


def _field_of(case_id: str) -> str:
    parts = case_id.split(".")
    return parts[1] if len(parts) >= 2 else case_id


@dataclass
class Finding:
    case_id: str
    category: str
    severity: str
    title: str
    basis: str
    suggestion: str
    evidence: str
    field: str = ""
    coarse: str = ""

    @property
    def root_cause_key(self) -> str:
        return f"{self.field}::{self.coarse}"


class Triage:
    def classify(self, summary: RunSummary) -> list[Finding]:
        return [f for f in (self._classify_one(r) for r in summary.broken) if f is not None]

    def _classify_one(self, result: CaseResult) -> Finding | None:
        message = result.message

        if any(kw in message for kw in _ENV_KEYWORDS):
            return Finding(
                case_id=result.name,
                category=ENV_ISSUE,
                severity=SEVERITY[ENV_ISSUE],
                title="无法连接被测服务或依赖超时",
                basis="environment",
                suggestion="确认被测服务、数据库与依赖中间件可用后重跑",
                evidence=message[:400],
            )

        match = _MARKER.search(message)
        if not match:
            return Finding(
                case_id=result.name,
                category=NEEDS_REVIEW,
                severity=SEVERITY[NEEDS_REVIEW],
                title="失败原因无法自动归因",
                basis="unknown",
                suggestion="请人工查看完整堆栈",
                evidence=message[:400],
            )

        kind = match.group("kind")
        case_id = match.group("case_id")
        basis = match.group("basis")
        title, suggestion = _lookup_pattern(basis)

        if kind == "generator":
            return Finding(
                case_id=case_id,
                category=CASE_ISSUE,
                severity=SEVERITY[CASE_ISSUE],
                title="生成的场景存在未解析变量",
                basis=basis,
                suggestion="生成侧变量捕获有问题，属于用例问题而非被测系统缺陷；已在解释器前置拦截",
                evidence=message[:400],
                field="",
                coarse="generator_unresolved",
            )

        if kind == "business":
            lowered = message.lower()
            if any(kw in message or kw in lowered for kw in _MISSING_DATA_KEYWORDS) and "实际 404" in message:
                return Finding(
                    case_id=case_id,
                    category=DATA_ISSUE,
                    severity=SEVERITY[DATA_ISSUE],
                    title="前置测试数据不存在",
                    basis=basis,
                    suggestion="检查测试数据字典是否与被测环境一致；生成侧不要凭空编造资源 ID",
                    evidence=message[:400],
                    field="",
                    coarse="data_missing",
                )
            return Finding(
                case_id=case_id,
                category=REAL_DEFECT,
                severity=SEVERITY[REAL_DEFECT],
                title=title,
                basis=basis,
                suggestion=suggestion,
                evidence=message[:400],
                field="",
                coarse=_coarse(basis),
            )

        expected = match.group("expected")
        actual = match.group("actual")
        expected_code = int(expected) if expected else None
        actual_code = int(actual) if actual else None

        category = CONTRACT_DRIFT
        if actual_code is not None and actual_code >= 500:
            category = REAL_DEFECT
            title, suggestion = "服务内部异常", "接口返回 5xx，建议排查服务端日志与异常处理"
        elif expected_code is not None and actual_code is not None:
            if expected_code >= 400 and actual_code < 400:
                # 本该拦截却放行 —— 缺校验，最典型的缺陷
                category = REAL_DEFECT
            elif expected_code < 400 and actual_code >= 400:
                # 本该成功却失败 —— 功能回归
                category = REAL_DEFECT
            else:
                category = CONTRACT_DRIFT

        return Finding(
            case_id=case_id,
            category=category,
            severity=SEVERITY[category],
            title=title,
            basis=basis,
            suggestion=suggestion,
            evidence=message[:400],
            field=_field_of(case_id),
            coarse=_coarse(basis),
        )

    @staticmethod
    def merge_by_root_cause(findings: list[Finding]) -> list[dict]:
        """按根因归并，输出「缺陷条目」而不是「失败用例条目」。

        同一字段的 minimum-1 和 maximum+1 属于同一个缺陷，
        不归并的话一个 bug 会刷出多行，报告的可信度直接崩塌。
        """
        merged: dict[str, dict] = {}
        for finding in findings:
            if finding.category != REAL_DEFECT:
                continue
            title, suggestion = _COARSE_SUMMARY.get(
                finding.coarse, (finding.title, finding.suggestion)
            )
            entry = merged.setdefault(
                finding.root_cause_key,
                {
                    "title": title,
                    "basis": finding.basis,
                    "severity": finding.severity,
                    "suggestion": suggestion,
                    "cases": [],
                },
            )
            entry["cases"].append(finding.case_id)
        return sorted(merged.values(), key=lambda e: (-len(e["cases"]), e["title"]))