"""执行器单元测试：跑 pytest、解析 junit-xml、收敛成结构化结果。

执行器读的是 junit-xml 而不是解析文本输出 —— 文本格式随 pytest 版本变化，
XML 稳定得多。这条决定值得被测试钉住：解析一旦错位，
通过率、失败用例、归因输入会一起错，而且不会报错。
"""
from __future__ import annotations

import pytest

from smarttest.runner import CaseResult, PytestRunner, RunSummary


class TestRunSummary:
    def test_counts_by_status(self):
        summary = RunSummary(
            results=[
                CaseResult("a", "passed", 0.1, ""),
                CaseResult("b", "failed", 0.2, "boom"),
                CaseResult("c", "errors", 0.3, "fixture 挂了"),
                CaseResult("d", "skipped", 0.0, ""),
            ]
        )
        assert summary.total == 4
        assert summary.passed == 1
        assert summary.failed == 1
        assert summary.errors == 1
        assert summary.skipped == 1
        assert [r.name for r in summary.broken] == ["b", "c"]
        # 必须用 approx：0.1+0.2+0.3 在 Python 3.11 上是 0.6000000000000001，
        # 3.12+ 改用了补偿求和才是精确的 0.6。写死浮点数会让门禁变成版本抽奖。
        assert summary.duration == pytest.approx(0.6)

    def test_empty_summary_is_all_zeros(self):
        summary = RunSummary()
        assert (summary.total, summary.passed, summary.failed, summary.duration) == (0, 0, 0, 0.0)
        assert summary.broken == []
        assert summary.report_path is None


class TestJunitParsing:
    def test_missing_report_yields_an_empty_summary(self, tmp_path):
        # junit 没落盘（例如 pytest 直接崩了）时不能抛异常，
        # 否则整条流水线会因为「报告文件不存在」而中断，掩盖真正的问题
        summary = PytestRunner(tmp_path, tmp_path)._parse(tmp_path / "nope.xml")
        assert summary.total == 0
        assert summary.report_path == tmp_path / "nope.xml"

    def test_run_and_parse_a_real_pytest_run(self, tmp_path):
        tests_dir = tmp_path / "generated"
        tests_dir.mkdir()
        (tests_dir / "test_sample.py").write_text(
            "def test_pass():\n"
            "    assert True\n"
            "\n"
            "\n"
            "def test_fail():\n"
            "    assert False, '断言消息里的线索'\n",
            encoding="utf-8",
        )
        report_dir = tmp_path / "reports"

        summary = PytestRunner(tests_dir, report_dir).run()

        assert summary.total == 2
        assert summary.passed == 1
        assert summary.failed == 1
        assert (report_dir / "junit.xml").exists()
        assert summary.report_path == report_dir / "junit.xml"
        # 失败消息要完整带出来 —— 归因模块全靠它判断「谁的问题」
        assert "断言消息里的线索" in summary.broken[0].message
        assert "passed" in summary.stdout or "1 failed" in summary.stdout

    def test_report_dir_is_created_on_demand(self, tmp_path):
        tests_dir = tmp_path / "generated"
        tests_dir.mkdir()
        (tests_dir / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        nested = tmp_path / "a" / "b" / "reports"
        summary = PytestRunner(tests_dir, nested).run()
        assert summary.passed == 1
        assert nested.exists()
