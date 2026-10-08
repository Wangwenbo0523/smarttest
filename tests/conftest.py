"""pytest 共享夹具。

测试文件位于 tests/ 子目录，pytest 默认只把该目录加进 sys.path，
这里显式把仓库根目录也加进去，保证 `import smarttest` 在本地和 CI 行为一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONTRACT = ROOT / "target_service" / "contract" / "openapi.yaml"


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def contract_path() -> Path:
    return CONTRACT


@pytest.fixture(scope="session")
def spec(contract_path: Path):
    """靶场契约解析出的 IR（只读，整个会话共用一份）。"""
    from smarttest.parser import OpenApiParser

    return OpenApiParser.from_file(contract_path).parse()


@pytest.fixture
def dp():
    """仓库自带的数据字典（product_id 有真实取值，order_id 没有）。"""
    from smarttest.dataprovider import DataProvider

    return DataProvider.load()


@pytest.fixture
def engine(dp):
    from smarttest.rules import RuleEngine

    return RuleEngine(dp)


@pytest.fixture
def make_operation():
    """工厂夹具：构造一个最小可用的 Operation，供不依赖真实契约的用例使用。"""

    def _make(**overrides):
        from smarttest.ir import ApiSpec, Operation

        kwargs = {
            "operation_id": "op",
            "method": "POST",
            "path": "/api/v1/things",
            "tag": "test",
            "summary": "",
            "responses": {"201": {"description": "ok"}},
        }
        kwargs.update(overrides)
        return ApiSpec(title="t", version="1", operations=[Operation(**kwargs)]).operations[0]

    return _make


@pytest.fixture
def make_summary():
    """工厂夹具：构造 RunSummary。

    results 里每项是 (用例名, 失败消息) 或 (用例名, 失败消息, 状态)，
    默认状态是 failed（环境/断言失败都走这一支）。
    """
    from smarttest.runner import CaseResult, RunSummary

    def _make(*results, duration: float = 1.0) -> RunSummary:
        items = []
        for item in results:
            name, message = item[0], item[1]
            status = item[2] if len(item) > 2 else "failed"
            items.append(CaseResult(name=name, status=status, duration=duration, message=message))
        return RunSummary(results=items)

    return _make


@pytest.fixture
def marker():
    """工厂夹具：拼装生成器写进断言消息的机器可读标记。"""

    def _make(case_id: str, basis: str, expected=None, actual=None, kind: str = "contract", tail: str = "") -> str:
        text = f"[kind={kind}][case_id={case_id}][basis={basis}]"
        if expected is not None:
            text += f"[expected={expected}]"
        if actual is not None:
            text += f"[actual={actual}]"
        if tail:
            text += f" | {tail}"
        return text

    return _make
