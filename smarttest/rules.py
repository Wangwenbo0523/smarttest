"""规则引擎：从 JSON Schema 确定性推导参数级用例。

三条设计原则（也是这个项目最该讲清楚的地方）：

1. 只做「能被 schema 推导」的用例
   必填、类型、长度、数值边界、枚举 —— 这些有唯一正确答案，
   用代码推导可以保证 100% 覆盖且完全可复现。交给模型只会引入不确定性。

2. 每个用例都带 design_basis 标签
   例如 boundary:minimum-1=0。有了标签才能统计「覆盖了什么设计方法」，
   也才能在失败时反推「是哪条规则发现的缺陷」。

3. 区分「拒绝侧」与「接受侧」
   - 拒绝侧（minLength-1、maximum+1 ...）：必然期望 4xx，断言无歧义。
   - 接受侧（minLength、minimum ...）：只有在该字段不引用外部资源时才断言成功。
     像 product_id 这种资源引用，合法格式不代表资源存在，
     所以它的取值一律来自数据字典，而不是凭空构造。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .dataprovider import DataProvider
from .ir import ApiSpec, Operation

JSON_MEDIA = "application/json"


@dataclass
class ValidationCase:
    """参数级用例：给一组输入，断言一个状态码。"""

    case_id: str
    operation_id: str
    description: str
    method: str
    path: str
    payload: Any
    headers: dict[str, str]
    expected_status: int
    design_basis: str

    def marker(self) -> str:
        """失败时写进断言消息的机器可读标记，供归因模块解析。"""
        return (
            f"[kind=contract][case_id={self.case_id}]"
            f"[basis={self.design_basis}][expected={self.expected_status}]"
        )


@dataclass
class FieldCoverage:
    """字段级覆盖统计，用于度量报告。"""

    field_name: str = ""
    operation_id: str = ""
    bases: set[str] = field(default_factory=set)


def _wrong_type_value(schema: dict[str, Any]) -> Any:
    """构造一个「类型一定不对」的值。"""
    return {
        "integer": "not-an-integer",
        "number": "not-a-number",
        "string": 12345,
        "boolean": "not-a-boolean",
        "array": "not-an-array",
        "object": "not-an-object",
    }.get(schema.get("type"), 12345)


def _valid_value(field_name: str, schema: dict[str, Any], dp: DataProvider) -> Any:
    """构造一个「合法」的值。优先取真实数据，其次取 schema 下界。"""
    if dp.has(field_name):
        return dp.first(field_name)
    if "enum" in schema:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    if "example" in schema:
        return schema["example"]

    kind = schema.get("type")
    if kind == "integer":
        return int(schema.get("minimum", 1))
    if kind == "number":
        return float(schema.get("minimum", 1))
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        return {}
    if kind == "string":
        length = max(int(schema.get("minLength", 1)), 1)
        return "a" * length
    return "value"


def build_valid_payload(op: Operation, dp: DataProvider) -> dict[str, Any]:
    """构造该接口的一份合法请求体。场景层也复用它，避免两处各写一套数据逻辑。"""
    props = (op.body_schema or {}).get("properties", {})
    return {name: _valid_value(name, sub, dp) for name, sub in props.items()}


class RuleEngine:
    def __init__(self, dp: DataProvider | None = None):
        self.dp = dp or DataProvider()
        self.coverage: dict[str, FieldCoverage] = {}

    # ---------- 对外入口 ----------

    def generate(self, spec: ApiSpec) -> list[ValidationCase]:
        cases: list[ValidationCase] = []
        for op in spec.operations:
            cases.extend(self._cases_for_operation(op))
        return cases

    # ---------- 单接口 ----------

    def _cases_for_operation(self, op: Operation) -> list[ValidationCase]:
        cases: list[ValidationCase] = []
        if op.body_schema:
            cases.extend(self._cases_for_body(op))
        cases.extend(self._cases_for_header_params(op))
        cases.extend(self._cases_for_path_params(op))
        return cases

    def _track(self, op: Operation, field_name: str, basis: str) -> None:
        cov = self.coverage.setdefault(field_name, FieldCoverage(field_name=field_name, operation_id=op.operation_id))
        cov.bases.add(basis)

    def _case(
        self,
        op: Operation,
        case_id: str,
        description: str,
        payload: Any,
        expected_status: int,
        design_basis: str,
        headers: dict[str, str] | None = None,
        path: str | None = None,
        field_name: str | None = None,
    ) -> ValidationCase:
        if field_name:
            self._track(op, field_name, design_basis.split("=")[0])
        return ValidationCase(
            case_id=f"{op.operation_id}.{case_id}",
            operation_id=op.operation_id,
            description=description,
            method=op.method,
            path=path or op.path,
            payload=payload,
            headers=headers or {},
            expected_status=expected_status,
            design_basis=design_basis,
        )

    # ---------- 请求体 ----------

    def _cases_for_body(self, op: Operation) -> list[ValidationCase]:
        schema = op.body_schema or {}
        props: dict[str, Any] = schema.get("properties", {})
        required: set[str] = set(schema.get("required", []))
        ok = op.success_status
        cases: list[ValidationCase] = []

        base = build_valid_payload(op, self.dp)

        # 0. 基准用例：全部合法，应当成功
        cases.append(
            self._case(op, "happy_path", "全部参数合法，应创建成功", dict(base), ok, "baseline:valid_payload")
        )

        for name, sub in props.items():
            is_resource = self.dp.has(name)

            # 1. 必填缺失
            if name in required:
                payload = {k: v for k, v in base.items() if k != name}
                cases.append(
                    self._case(op, f"{name}.required_missing", f"缺少必填字段 {name}", payload, 422,
                               "required:missing", field_name=name)
                )

            # 2. 类型错误
            payload = dict(base)
            payload[name] = _wrong_type_value(sub)
            cases.append(
                self._case(op, f"{name}.type_mismatch", f"{name} 类型错误", payload, 422,
                           f"type:{sub.get('type')}=wrong", field_name=name)
            )

            # 3. 字符串长度边界
            if sub.get("type") == "string":
                mn, mx = sub.get("minLength"), sub.get("maxLength")
                if isinstance(mn, int) and mn > 0:
                    payload = dict(base)
                    payload[name] = "a" * (mn - 1)
                    cases.append(
                        self._case(op, f"{name}.minLength-1", f"{name} 长度为 {mn-1}（下界-1）", payload, 422,
                                   f"boundary:minLength-1={mn-1}", field_name=name)
                    )
                if isinstance(mx, int):
                    payload = dict(base)
                    payload[name] = "a" * (mx + 1)
                    cases.append(
                        self._case(op, f"{name}.maxLength+1", f"{name} 长度为 {mx+1}（上界+1）", payload, 422,
                                   f"boundary:maxLength+1={mx+1}", field_name=name)
                    )
                if isinstance(mx, int) and not is_resource:
                    # 上界本身（接受侧）。只测「上界+1 被拒绝」是不够的 ——
                    # 还得证明「刚好取到上界时能正常通过」，否则边界覆盖是残的。
                    payload = dict(base)
                    payload[name] = "a" * mx
                    cases.append(
                        self._case(op, f"{name}.maxLength", f"{name} 长度为 {mx}（上界本身）", payload, ok,
                                   f"boundary:maxLength={mx}", field_name=name)
                    )
                if isinstance(mn, int) and mn > 0 and not is_resource:
                    payload = dict(base)
                    payload[name] = "a" * mn
                    cases.append(
                        self._case(op, f"{name}.minLength", f"{name} 长度为 {mn}（下界）", payload, ok,
                                   f"boundary:minLength={mn}", field_name=name)
                    )

            # 4. 数值边界
            if sub.get("type") in ("integer", "number"):
                mn, mx = sub.get("minimum"), sub.get("maximum")
                if mn is not None:
                    payload = dict(base)
                    payload[name] = mn - 1
                    cases.append(
                        self._case(op, f"{name}.minimum-1", f"{name} 取 {mn-1}（下界-1）", payload, 422,
                                   f"boundary:minimum-1={mn-1}", field_name=name)
                    )
                if mx is not None:
                    payload = dict(base)
                    payload[name] = mx + 1
                    cases.append(
                        self._case(op, f"{name}.maximum+1", f"{name} 取 {mx+1}（上界+1）", payload, 422,
                                   f"boundary:maximum+1={mx+1}", field_name=name)
                    )
                if not is_resource:
                    if mn is not None:
                        payload = dict(base)
                        payload[name] = mn
                        cases.append(
                            self._case(op, f"{name}.minimum", f"{name} 取 {mn}（下界）", payload, ok,
                                       f"boundary:minimum={mn}", field_name=name)
                        )
                    if mx is not None:
                        payload = dict(base)
                        payload[name] = mx
                        cases.append(
                            self._case(op, f"{name}.maximum", f"{name} 取 {mx}（上界）", payload, ok,
                                       f"boundary:maximum={mx}", field_name=name)
                        )

            # 5. 枚举
            if "enum" in sub:
                for value in sub["enum"]:
                    payload = dict(base)
                    payload[name] = value
                    cases.append(
                        self._case(op, f"{name}.enum.{value}", f"{name} 取合法枚举 {value}", payload, ok,
                                   f"enum:valid={value}", field_name=name)
                    )
                payload = dict(base)
                payload[name] = "__INVALID_ENUM__"
                cases.append(
                    self._case(op, f"{name}.enum.invalid", f"{name} 取非法枚举值", payload, 422,
                               "enum:invalid", field_name=name)
                )

            # 6. 可空字段传 null
            if sub.get("nullable") and name not in required:
                payload = dict(base)
                payload[name] = None
                cases.append(
                    self._case(op, f"{name}.null", f"{name} 显式传 null（契约允许）", payload, ok,
                               "nullable:null", field_name=name)
                )

            # 7. 资源引用不存在
            if is_resource:
                unknown_value = self.dp.unknown_value(name)
                if unknown_value is not None:
                    payload = dict(base)
                    payload[name] = unknown_value
                    cases.append(
                        self._case(op, f"{name}.not_found", f"{name} 指向不存在的资源", payload, 404,
                                   "resource:not_found", field_name=name)
                    )

        return cases

    # ---------- 请求头参数 ----------

    def _cases_for_header_params(self, op: Operation) -> list[ValidationCase]:
        """请求头参数用例。

        这一类之前整块缺失，是评测集把它点出来的（见 evals/ground_truth.yaml）——
        这正好说明标注集的价值：它能让「还有哪没覆盖」变成已知，而不是靠灵感。
        """
        cases: list[ValidationCase] = []
        if not op.header_params():
            return cases

        ok = op.success_status
        base_payload = build_valid_payload(op, self.dp) if op.body_schema else None

        for param in op.header_params():
            if param.required:
                cases.append(
                    self._case(op, f"{param.name}.required_missing", f"缺少必填请求头 {param.name}",
                               base_payload, 422, "required:missing_header",
                               headers={}, field_name=param.name)
                )

            mx = param.schema.get("maxLength")
            if isinstance(mx, int):
                cases.append(
                    self._case(op, f"{param.name}.maxLength+1",
                               f"请求头 {param.name} 长度为 {mx+1}（上界+1）",
                               base_payload, 422, f"boundary:maxLength+1={mx+1}",
                               headers={param.name: "a" * (mx + 1)}, field_name=param.name)
                )
                cases.append(
                    self._case(op, f"{param.name}.maxLength",
                               f"请求头 {param.name} 长度为 {mx}（上界本身）",
                               base_payload, ok, f"boundary:maxLength={mx}",
                               headers={param.name: "a" * mx}, field_name=param.name)
                )

            mn = param.schema.get("minLength")
            if isinstance(mn, int) and mn > 1:
                cases.append(
                    self._case(op, f"{param.name}.minLength-1",
                               f"请求头 {param.name} 长度为 {mn-1}（下界-1）",
                               base_payload, 422, f"boundary:minLength-1={mn-1}",
                               headers={param.name: "a" * (mn - 1)}, field_name=param.name)
                )

        return cases

    # ---------- 路径参数 ----------

    def _cases_for_path_params(self, op: Operation) -> list[ValidationCase]:
        cases: list[ValidationCase] = []
        if not op.path_params():
            return cases
        ok = op.success_status

        for param in op.path_params():
            real = self.dp.first(param.name)
            # 只有「资源存在」这一条依赖真实数据；其余用例（资源不存在、长度越界）
            # 不依赖数据，必须照常生成，否则接口覆盖率会被测试数据卡住。
            if real is not None:
                resolved = op.path.replace("{" + param.name + "}", str(real))
                cases.append(
                    self._case(op, f"{param.name}.exists", f"{param.name} 使用真实存在的资源",
                               None, ok, "resource:exists", path=resolved, field_name=param.name)
                )

            unknown_value = self.dp.unknown_value(param.name)
            if unknown_value is not None:
                resolved_unknown = op.path.replace("{" + param.name + "}", str(unknown_value))
                cases.append(
                    self._case(op, f"{param.name}.not_found", f"{param.name} 指向不存在的资源",
                               None, 404, "resource:not_found", path=resolved_unknown, field_name=param.name)
                )

            mn, mx = param.schema.get("minLength"), param.schema.get("maxLength")
            if isinstance(mx, int):
                resolved_long = op.path.replace("{" + param.name + "}", "a" * (mx + 1))
                cases.append(
                    self._case(op, f"{param.name}.maxLength+1", f"{param.name} 超过最大长度",
                               None, 422, f"boundary:maxLength+1={mx+1}", path=resolved_long, field_name=param.name)
                )
            if isinstance(mn, int) and mn > 1:
                resolved_short = op.path.replace("{" + param.name + "}", "a" * (mn - 1))
                cases.append(
                    self._case(op, f"{param.name}.minLength-1", f"{param.name} 低于最小长度",
                               None, 422, f"boundary:minLength-1={mn-1}", path=resolved_short, field_name=param.name)
                )

        return cases