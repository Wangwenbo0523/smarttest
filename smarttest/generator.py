"""脚本生成器：把用例渲染成可读、可维护的 pytest 代码。

生成策略：模板负责「结构」，模型只负责「填空」。
结构稳定 = 产出可维护，这也是不直接让 LLM 吐整个文件的原因：
让模型写整个文件，产出的代码风格漂移、不可 review，半年后没人敢改。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from .ir import ApiSpec
from .rules import ValidationCase

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


class PytestGenerator:
    def __init__(self, template_dir: str | Path | None = None, base_url: str = "http://127.0.0.1:8123"):
        self.base_url = base_url
        # operationId -> Operation，渲染场景时用来把引用解析成真实 URL
        self._operations: dict[str, Any] = {}
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir or TEMPLATE_DIR)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        # 用 repr 而不是 tojson：生成的必须是合法 Python 字面量，
        # JSON 的 null/true/false 在 Python 里并不存在。
        self.env.filters["pyrepr"] = repr

    @staticmethod
    def _basis_summary(cases: list[ValidationCase]) -> dict[str, int]:
        counter: Counter[str] = Counter()
        for case in cases:
            counter[case.design_basis.split("=")[0]] += 1
        return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))

    def render_contract_tests(self, spec: ApiSpec, cases: list[ValidationCase], out_dir: str | Path) -> Path:
        self._operations = {op.operation_id: op for op in spec.operations}
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        code = self.env.get_template("contract_test.py.j2").render(
            spec_title=spec.title,
            spec_version=spec.version,
            base_url=self.base_url,
            cases=cases,
            basis_summary=self._basis_summary(cases),
        )
        target = out / "test_contract_generated.py"
        target.write_text(code, encoding="utf-8")
        return target

    def render_business_tests(
        self,
        out_dir: str | Path,
        template_name: str = "business_test.py.j2",
        context: dict[str, Any] | None = None,
    ) -> Path:
        """渲染业务层用例。

        业务规则需要领域知识，必然一个靶场一套模板；生成器不内置任何一种，
        模板名与上下文都由靶场注册表提供（见 smarttest/targets.py）。
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        code = self.env.get_template(template_name).render(
            base_url=self.base_url, **(context or {})
        )
        target = out / "test_business_generated.py"
        target.write_text(code, encoding="utf-8")
        return target

    def render_scenario_tests(
        self,
        scenarios: list,
        out_dir: str | Path,
        provider_name: str = "rule-based",
        degraded_reason: str = "",
    ) -> Path:
        """渲染场景测试：模型产出数据，解释器确定性执行。

        场景与代码分离的好处：模型无法把语法错误带进测试套件，
        新增场景也不需要重新生成代码。
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)

        # 在这里把 operationId 解析成真实 URL —— 模型编不出不存在的接口
        rendered: list[dict] = []
        for scenario in scenarios:
            steps = []
            for step in scenario.steps:
                steps.append(
                    {
                        "operation_id": step.operation_id,
                        "method": self._method_of(step.operation_id),
                        "path": self._path_of(step.operation_id, step.path_params),
                        "headers": step.headers,
                        "body": step.body,
                        "capture": step.capture,
                        "expect_body": step.expect_body,
                        "expect_status": step.expect_status,
                    }
                )
            rendered.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "title": scenario.title,
                    "rationale": scenario.rationale,
                    "source": scenario.source,
                    "steps": steps,
                }
            )

        (out / "scenarios.json").write_text(
            json.dumps(rendered, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        code = self.env.get_template("scenario_runner.py.j2").render(
            base_url=self.base_url,
            scenarios=rendered,
            provider_name=provider_name,
            degraded_reason=degraded_reason,
        )
        target = out / "test_scenarios_generated.py"
        target.write_text(code, encoding="utf-8")
        return target

    def _method_of(self, operation_id: str) -> str:
        return self._operations[operation_id].method

    def _path_of(self, operation_id: str, path_params: dict) -> str:
        path = self._operations[operation_id].path
        for name, value in (path_params or {}).items():
            path = path.replace("{" + name + "}", str(value))
        return path

    def render_conftest(self, out_dir: str | Path) -> Path:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        target = out / "conftest.py"
        target.write_text(self.env.get_template("conftest.py.j2").render(), encoding="utf-8")
        return target
