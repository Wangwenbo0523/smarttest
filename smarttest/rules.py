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

import copy
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
    params: dict[str, Any] = field(default_factory=dict)

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


@dataclass
class FieldTarget:
    """请求体里要逐字段覆盖的目标：从根开始的路径 + 它自己的约束。"""

    path: tuple[str, ...]
    schema: dict[str, Any]
    required: bool
    is_resource: bool

    @property
    def label(self) -> str:
        """写进 case_id 的名字。嵌套字段是 author.name 这种点分路径。"""
        return ".".join(self.path)

    @property
    def leaf(self) -> str:
        """数据字典按字段名查，取路径最后一段。"""
        return self.path[-1]


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


_MAX_NESTING = 3


def _valid_value(field_name: str, schema: dict[str, Any], dp: DataProvider, depth: int = 0) -> Any:
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
        # 数组至少要凑够 minItems 个元素，否则「合法请求」本身就是非法的
        item_schema = schema.get("items") or {}
        return [
            _valid_value(f"{field_name}_item", item_schema, dp, depth + 1)
            for _ in range(max(int(schema.get("minItems", 0)), 0))
        ]
    if kind == "object":
        # 嵌套对象要按它自己的 properties 递归构造：给个空字典的话，
        # 契约里必填的嵌套字段会直接把基准用例打成失败。
        props = schema.get("properties") or {}
        if depth >= _MAX_NESTING or not props:
            return {}
        return {name: _valid_value(name, sub, dp, depth + 1) for name, sub in props.items()}
    if kind == "string":
        length = max(int(schema.get("minLength", 1)), 1)
        return "a" * length
    return "value"


def build_valid_payload(op: Operation, dp: DataProvider) -> dict[str, Any]:
    """构造该接口的一份合法请求体。场景层也复用它，避免两处各写一套数据逻辑。"""
    props = (op.body_schema or {}).get("properties", {})
    return {name: _valid_value(name, sub, dp) for name, sub in props.items()}


def case_field_path(case_id: str) -> str:
    """从 case_id 里取出字段路径（去掉 operationId 前缀与用例后缀）。

    createOrder.quantity.minimum-1         -> quantity
    createArticle.author.name.maxLength+1  -> author.name
    createArticle.status.enum.DRAFT        -> status
    scn_round_trip                         -> scn_round_trip

    评测与失败归因都用它当字段名。嵌套字段必须保留点分路径 ——
    否则 author.name 与 author.email 会被算成同一个字段，
    一个缺陷会盖住另一个。
    """
    parts = case_id.split(".")
    if len(parts) < 2:
        return case_id
    body = parts[1:]
    # 枚举用例的后缀是「enum + 取值」两段，其余用例都是一段
    if len(body) >= 2 and body[-2] == "enum":
        body = body[:-2]
    else:
        body = body[:-1]
    return ".".join(body) if body else case_id


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
        base_headers = self._required_headers(op)
        if op.body_schema:
            cases.extend(self._cases_for_body(op, base_headers))
        cases.extend(self._cases_for_query_params(op, base_headers))
        cases.extend(self._cases_for_header_params(op, base_headers))
        cases.extend(self._cases_for_path_params(op, base_headers))
        return cases

    def _required_headers(self, op: Operation) -> dict[str, str]:
        """必填请求头的合法取值。

        契约声明了必填请求头时，**任何**用例都必须带上它 —— 否则被测服务会先
        因为「缺请求头」返回 422，那条用例就不再是在测它原本要测的字段了。

        这个缺口是第一靶场（订单服务）照不出来的：它唯一的请求头 Idempotency-Key
        是可选的。第二个靶场（用户服务）声明了必填的 X-Tenant-Id，规则一跑就露了。
        """
        return {
            param.name: str(_valid_value(param.name, param.schema, self.dp))
            for param in op.header_params()
            if param.required
        }

    @staticmethod
    def _with_base_headers(
        cases: list[ValidationCase], base_headers: dict[str, str]
    ) -> list[ValidationCase]:
        """给一批用例统一补上必填请求头；用例自己声明的请求头优先。"""
        if not base_headers:
            return cases
        for case in cases:
            case.headers = {**base_headers, **case.headers}
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
        params: dict[str, Any] | None = None,
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
            params=params or {},
        )

    # ---------- 请求体 ----------

    def _cases_for_body(
        self, op: Operation, base_headers: dict[str, str] | None = None
    ) -> list[ValidationCase]:
        base_headers = self._required_headers(op) if base_headers is None else base_headers
        ok = op.success_status
        cases: list[ValidationCase] = []

        base = build_valid_payload(op, self.dp)

        # 0. 基准用例：全部合法，应当成功
        cases.append(
            self._case(op, "happy_path", "全部参数合法，应创建成功", dict(base), ok, "baseline:valid_payload")
        )

        for target in self._body_targets(op, base):
            name = target.label
            sub = target.schema
            is_resource = target.is_resource

            # 1. 必填缺失
            if target.required:
                cases.append(
                    self._case(op, f"{name}.required_missing", f"缺少必填字段 {name}",
                               self._drop_path(base, target.path), 422,
                               "required:missing", field_name=name)
                )

            # 2. 类型错误
            cases.append(
                self._case(op, f"{name}.type_mismatch", f"{name} 类型错误",
                           self._set_path(base, target.path, _wrong_type_value(sub)), 422,
                           f"type:{sub.get('type')}=wrong", field_name=name)
            )

            # 3. 字符串长度边界
            if sub.get("type") == "string":
                mn, mx = sub.get("minLength"), sub.get("maxLength")
                if isinstance(mn, int) and mn > 0:
                    cases.append(
                        self._case(op, f"{name}.minLength-1", f"{name} 长度为 {mn-1}（下界-1）",
                                   self._set_path(base, target.path, "a" * (mn - 1)), 422,
                                   f"boundary:minLength-1={mn-1}", field_name=name)
                    )
                if isinstance(mx, int):
                    cases.append(
                        self._case(op, f"{name}.maxLength+1", f"{name} 长度为 {mx+1}（上界+1）",
                                   self._set_path(base, target.path, "a" * (mx + 1)), 422,
                                   f"boundary:maxLength+1={mx+1}", field_name=name)
                    )
                if isinstance(mx, int) and not is_resource:
                    # 上界本身（接受侧）。只测「上界+1 被拒绝」是不够的 ——
                    # 还得证明「刚好取到上界时能正常通过」，否则边界覆盖是残的。
                    cases.append(
                        self._case(op, f"{name}.maxLength", f"{name} 长度为 {mx}（上界本身）",
                                   self._set_path(base, target.path, "a" * mx), ok,
                                   f"boundary:maxLength={mx}", field_name=name)
                    )
                if isinstance(mn, int) and mn > 0 and not is_resource:
                    cases.append(
                        self._case(op, f"{name}.minLength", f"{name} 长度为 {mn}（下界）",
                                   self._set_path(base, target.path, "a" * mn), ok,
                                   f"boundary:minLength={mn}", field_name=name)
                    )

            # 4. 数值边界
            if sub.get("type") in ("integer", "number"):
                mn, mx = sub.get("minimum"), sub.get("maximum")
                if mn is not None:
                    cases.append(
                        self._case(op, f"{name}.minimum-1", f"{name} 取 {mn-1}（下界-1）",
                                   self._set_path(base, target.path, mn - 1), 422,
                                   f"boundary:minimum-1={mn-1}", field_name=name)
                    )
                if mx is not None:
                    cases.append(
                        self._case(op, f"{name}.maximum+1", f"{name} 取 {mx+1}（上界+1）",
                                   self._set_path(base, target.path, mx + 1), 422,
                                   f"boundary:maximum+1={mx+1}", field_name=name)
                    )
                if not is_resource:
                    if mn is not None:
                        cases.append(
                            self._case(op, f"{name}.minimum", f"{name} 取 {mn}（下界）",
                                       self._set_path(base, target.path, mn), ok,
                                       f"boundary:minimum={mn}", field_name=name)
                        )
                    if mx is not None:
                        cases.append(
                            self._case(op, f"{name}.maximum", f"{name} 取 {mx}（上界）",
                                       self._set_path(base, target.path, mx), ok,
                                       f"boundary:maximum={mx}", field_name=name)
                        )

            # 5. 枚举
            if "enum" in sub:
                for value in sub["enum"]:
                    cases.append(
                        self._case(op, f"{name}.enum.{value}", f"{name} 取合法枚举 {value}",
                                   self._set_path(base, target.path, value), ok,
                                   f"enum:valid={value}", field_name=name)
                    )
                cases.append(
                    self._case(op, f"{name}.enum.invalid", f"{name} 取非法枚举值",
                               self._set_path(base, target.path, "__INVALID_ENUM__"), 422,
                               "enum:invalid", field_name=name)
                )

            # 6. 可空字段传 null
            if sub.get("nullable") and not target.required:
                cases.append(
                    self._case(op, f"{name}.null", f"{name} 显式传 null（契约允许）",
                               self._set_path(base, target.path, None), ok,
                               "nullable:null", field_name=name)
                )

            # 7. 资源引用不存在
            if is_resource:
                unknown_value = self.dp.unknown_value(target.leaf)
                if unknown_value is not None:
                    cases.append(
                        self._case(op, f"{name}.not_found", f"{name} 指向不存在的资源",
                                   self._set_path(base, target.path, unknown_value), 404,
                                   "resource:not_found", field_name=name)
                    )

        return self._with_base_headers(cases, base_headers)

    # ---------- 请求体字段目标 ----------

    def _body_targets(self, op: Operation, base: dict[str, Any]) -> list[FieldTarget]:
        """展开请求体里需要逐字段覆盖的目标。

        顶层字段总是展开；对象字段再往里走一层 —— 嵌套对象的必填、类型、
        长度与数值边界同样能被测到。深度上限设成两层是刻意的：再往下用例
        数量会成倍增长，而真实契约里三层以上的必填嵌套很少见。

        这个能力是第三靶场（内容服务）逼出来的：它的请求体里有个必填的
        author 对象，只测顶层字段等于把里面两个必填字段整块漏掉。
        """
        schema = op.body_schema or {}
        required = set(schema.get("required", []))
        targets: list[FieldTarget] = []

        for name, sub in (schema.get("properties") or {}).items():
            targets.append(self._field_target((name,), sub, name in required))
            if sub.get("type") == "object" and isinstance(base.get(name), dict):
                nested_required = set(sub.get("required", []))
                for nested_name, nested_schema in (sub.get("properties") or {}).items():
                    targets.append(
                        self._field_target(
                            (name, nested_name), nested_schema, nested_name in nested_required
                        )
                    )
        return targets

    def _field_target(
        self, path: tuple[str, ...], schema: dict[str, Any], required: bool
    ) -> FieldTarget:
        return FieldTarget(
            path=path, schema=schema, required=required, is_resource=self.dp.has(path[-1])
        )

    @staticmethod
    def _set_path(base: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
        """复制请求体，并把某个（可能嵌套的）字段设成指定值。

        每个用例都拿一份独立的深拷贝：用例之间不能共享可变结构，
        否则一条用例的改动会渗到另一条上去，失败归因就会指错字段。
        """
        payload = copy.deepcopy(base)
        cursor = payload
        for key in path[:-1]:
            cursor = cursor[key]
        cursor[path[-1]] = value
        return payload

    @staticmethod
    def _drop_path(base: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
        """复制请求体并删掉某个（可能嵌套的）字段，用于「必填缺失」用例。"""
        payload = copy.deepcopy(base)
        cursor = payload
        for key in path[:-1]:
            cursor = cursor[key]
        cursor.pop(path[-1], None)
        return payload

    # ---------- 查询参数 ----------

    def _cases_for_query_params(
        self, op: Operation, base_headers: dict[str, str] | None = None
    ) -> list[ValidationCase]:
        """查询参数用例。

        这一类之前整块缺失：`location: query` 的参数既不生成用例，生成的请求
        也不会带上它们。带列表接口的契约（分页、过滤、搜索）因此完全没有覆盖，
        而且不会报错 —— 第三靶场（内容服务）把它逼了出来。

        与请求体有一处差别：query 参数在线上都是字符串，「类型错误」只对数值
        与布尔参数才有意义；字符串参数不可能类型错，只有长度和枚举。
        """
        cases: list[ValidationCase] = []
        params = op.query_params()
        if not params:
            return cases
        if base_headers is None:
            base_headers = self._required_headers(op)

        ok = op.success_status
        base_payload = build_valid_payload(op, self.dp) if op.body_schema else None
        # 必填查询参数要出现在其余每一条用例里，理由与必填请求头相同
        base_params = {
            p.name: _valid_value(p.name, p.schema, self.dp) for p in params if p.required
        }

        for param in params:
            others = {k: v for k, v in base_params.items() if k != param.name}
            kind = param.schema.get("type")
            is_resource = self.dp.has(param.name)

            if param.required:
                cases.append(
                    self._case(op, f"{param.name}.required_missing",
                               f"缺少必填查询参数 {param.name}", base_payload, 422,
                               "required:missing_query", params=others, field_name=param.name)
                )

            if kind in ("integer", "number", "boolean"):
                cases.append(
                    self._case(op, f"{param.name}.type_mismatch",
                               f"查询参数 {param.name} 类型错误", base_payload, 422,
                               f"type:{kind}=wrong",
                               params={**others, param.name: _wrong_type_value(param.schema)},
                               field_name=param.name)
                )

            if kind == "string":
                mn, mx = param.schema.get("minLength"), param.schema.get("maxLength")
                if isinstance(mn, int) and mn > 0:
                    cases.append(
                        self._case(op, f"{param.name}.minLength-1",
                                   f"查询参数 {param.name} 长度为 {mn-1}（下界-1）",
                                   base_payload, 422, f"boundary:minLength-1={mn-1}",
                                   params={**others, param.name: "a" * (mn - 1)},
                                   field_name=param.name)
                    )
                if isinstance(mx, int):
                    cases.append(
                        self._case(op, f"{param.name}.maxLength+1",
                                   f"查询参数 {param.name} 长度为 {mx+1}（上界+1）",
                                   base_payload, 422, f"boundary:maxLength+1={mx+1}",
                                   params={**others, param.name: "a" * (mx + 1)},
                                   field_name=param.name)
                    )
                if isinstance(mx, int) and not is_resource:
                    cases.append(
                        self._case(op, f"{param.name}.maxLength",
                                   f"查询参数 {param.name} 长度为 {mx}（上界本身）",
                                   base_payload, ok, f"boundary:maxLength={mx}",
                                   params={**others, param.name: "a" * mx},
                                   field_name=param.name)
                    )
                if isinstance(mn, int) and mn > 0 and not is_resource:
                    cases.append(
                        self._case(op, f"{param.name}.minLength",
                                   f"查询参数 {param.name} 长度为 {mn}（下界）",
                                   base_payload, ok, f"boundary:minLength={mn}",
                                   params={**others, param.name: "a" * mn},
                                   field_name=param.name)
                    )

            if kind in ("integer", "number"):
                mn, mx = param.schema.get("minimum"), param.schema.get("maximum")
                if mn is not None:
                    cases.append(
                        self._case(op, f"{param.name}.minimum-1",
                                   f"查询参数 {param.name} 取 {mn-1}（下界-1）",
                                   base_payload, 422, f"boundary:minimum-1={mn-1}",
                                   params={**others, param.name: mn - 1},
                                   field_name=param.name)
                    )
                if mx is not None:
                    cases.append(
                        self._case(op, f"{param.name}.maximum+1",
                                   f"查询参数 {param.name} 取 {mx+1}（上界+1）",
                                   base_payload, 422, f"boundary:maximum+1={mx+1}",
                                   params={**others, param.name: mx + 1},
                                   field_name=param.name)
                    )
                if not is_resource:
                    if mn is not None:
                        cases.append(
                            self._case(op, f"{param.name}.minimum",
                                       f"查询参数 {param.name} 取 {mn}（下界）",
                                       base_payload, ok, f"boundary:minimum={mn}",
                                       params={**others, param.name: mn},
                                       field_name=param.name)
                        )
                    if mx is not None:
                        cases.append(
                            self._case(op, f"{param.name}.maximum",
                                       f"查询参数 {param.name} 取 {mx}（上界）",
                                       base_payload, ok, f"boundary:maximum={mx}",
                                       params={**others, param.name: mx},
                                       field_name=param.name)
                        )

            if "enum" in param.schema:
                for value in param.schema["enum"]:
                    cases.append(
                        self._case(op, f"{param.name}.enum.{value}",
                                   f"查询参数 {param.name} 取合法枚举 {value}",
                                   base_payload, ok, f"enum:valid={value}",
                                   params={**others, param.name: value},
                                   field_name=param.name)
                    )
                cases.append(
                    self._case(op, f"{param.name}.enum.invalid",
                               f"查询参数 {param.name} 取非法枚举值",
                               base_payload, 422, "enum:invalid",
                               params={**others, param.name: "__INVALID_ENUM__"},
                               field_name=param.name)
                )

        return self._with_base_headers(cases, base_headers)

    # ---------- 请求头参数 ----------

    def _cases_for_header_params(
        self, op: Operation, base_headers: dict[str, str] | None = None
    ) -> list[ValidationCase]:
        """请求头参数用例。

        这一类之前整块缺失，是评测集把它点出来的（见 evals/ground_truth.yaml）——
        这正好说明标注集的价值：它能让「还有哪没覆盖」变成已知，而不是靠灵感。
        """
        cases: list[ValidationCase] = []
        if not op.header_params():
            return cases
        if base_headers is None:
            base_headers = self._required_headers(op)

        ok = op.success_status
        base_payload = build_valid_payload(op, self.dp) if op.body_schema else None

        for param in op.header_params():
            # 其余必填请求头照常带上；被测的这个单独构造，避免和「缺必填头」混在一起。
            others = {k: v for k, v in base_headers.items() if k != param.name}

            if param.required:
                cases.append(
                    self._case(op, f"{param.name}.required_missing", f"缺少必填请求头 {param.name}",
                               base_payload, 422, "required:missing_header",
                               headers=others, field_name=param.name)
                )

            mx = param.schema.get("maxLength")
            if isinstance(mx, int):
                cases.append(
                    self._case(op, f"{param.name}.maxLength+1",
                               f"请求头 {param.name} 长度为 {mx+1}（上界+1）",
                               base_payload, 422, f"boundary:maxLength+1={mx+1}",
                               headers={**others, param.name: "a" * (mx + 1)}, field_name=param.name)
                )
                cases.append(
                    self._case(op, f"{param.name}.maxLength",
                               f"请求头 {param.name} 长度为 {mx}（上界本身）",
                               base_payload, ok, f"boundary:maxLength={mx}",
                               headers={**others, param.name: "a" * mx}, field_name=param.name)
                )

            mn = param.schema.get("minLength")
            if isinstance(mn, int) and mn > 1:
                cases.append(
                    self._case(op, f"{param.name}.minLength-1",
                               f"请求头 {param.name} 长度为 {mn-1}（下界-1）",
                               base_payload, 422, f"boundary:minLength-1={mn-1}",
                               headers={**others, param.name: "a" * (mn - 1)}, field_name=param.name)
                )

        return cases

    # ---------- 路径参数 ----------

    def _cases_for_path_params(
        self, op: Operation, base_headers: dict[str, str] | None = None
    ) -> list[ValidationCase]:
        cases: list[ValidationCase] = []
        if not op.path_params():
            return cases
        base_headers = self._required_headers(op) if base_headers is None else base_headers
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

        return self._with_base_headers(cases, base_headers)
