"""成果卡的一致性测试。

图是提交进仓库的二进制产物，最容易出的问题是**图里的数字和实际结果脱节**
（改了规则、重跑了靶场，却忘了重画图）。

这里把卡片脚本里的数字钉在真实生成结果上：契约用例数必须与规则引擎当场跑出来的
条数一致，合计行必须等于明细行之和。图本身要用 `python tools/render_results_card.py`
重画 —— 测试负责告诉你「该重画了」。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from smarttest.rules import RuleEngine

CARD_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "render_results_card.py"


def load_card_module():
    spec = importlib.util.spec_from_file_location("render_results_card", CARD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_card_script_exists_and_output_is_committed():
    assert CARD_SCRIPT.exists()
    assert (CARD_SCRIPT.parent.parent / "docs" / "results-card.png").exists()


@pytest.mark.parametrize(("index", "target_key"), [(0, "orders"), (1, "users"), (2, "articles")])
def test_case_counts_match_what_the_engine_actually_generates(index, target_key, target_assets):
    module = load_card_module()
    spec, dp = target_assets(target_key)

    declared = module.TARGETS[index][2]
    actual = len(RuleEngine(dp).generate(spec))

    assert declared == str(actual), (
        f"{target_key} 的契约用例数变了（卡片写 {declared}，实际 {actual}）—— "
        f"请重跑 python tools/render_results_card.py 并同步文档"
    )


def test_total_row_equals_the_sum_of_detail_rows():
    module = load_card_module()
    assert module.TOTAL[2] == str(sum(int(row[2]) for row in module.TARGETS))
    assert module.TOTAL[3] == str(sum(int(row[3]) for row in module.TARGETS))
    assert module.TOTAL[4] == str(sum(int(row[4]) for row in module.TARGETS))


def test_fixed_build_column_is_all_zeros():
    # 「修复版必须归零」是这个项目的核心主张，图里不能出现非零
    module = load_card_module()
    assert {row[4] for row in module.TARGETS} == {"0"}
    assert module.TOTAL[4] == "0"


def test_kpis_agree_with_the_table():
    module = load_card_module()
    labels = {kpi[0]: kpi[2] for kpi in module.KPIS}
    assert labels["契约用例"] == module.TOTAL[2]
    assert labels["检出缺陷"] == module.TOTAL[3]
    assert labels["修复版残留"] == module.TOTAL[4]
