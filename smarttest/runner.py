"""执行器：跑 pytest，并把结果收敛成结构化数据。

用 junit-xml 而不是解析文本输出：文本格式随 pytest 版本变化，
解析 XML 稳定得多，这是「确定性优先」原则在工程上的体现。
"""
from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_STATUS_MAP = {"failure": "failed", "error": "errors", "skipped": "skipped"}


@dataclass
class CaseResult:
    name: str
    status: str  # passed | failed | errors | skipped
    duration: float
    message: str


@dataclass
class RunSummary:
    results: list[CaseResult] = field(default_factory=list)
    stdout: str = ""
    report_path: Path | None = None

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.status == "errors")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped")

    @property
    def duration(self) -> float:
        return sum(r.duration for r in self.results)

    @property
    def broken(self) -> list[CaseResult]:
        return [r for r in self.results if r.status in ("failed", "errors")]


class PytestRunner:
    def __init__(self, tests_dir: str | Path, report_dir: str | Path, timeout: int = 300):
        self.tests_dir = Path(tests_dir)
        self.report_dir = Path(report_dir)
        self.timeout = timeout

    def run(self) -> RunSummary:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        report = self.report_dir / "junit.xml"

        cmd = [
            sys.executable, "-m", "pytest", str(self.tests_dir),
            "-q", "--no-header", "-p", "no:cacheprovider",
            f"--junitxml={report}",
        ]
        # 必须显式指定 utf-8：Windows 默认用 GBK 解码子进程输出，
        # 遇到中文用例名会直接抛 UnicodeDecodeError。
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            cwd=str(self.tests_dir),
        )

        summary = self._parse(report)
        summary.stdout = (proc.stdout or "") + (proc.stderr or "")
        return summary

    def _parse(self, report: Path) -> RunSummary:
        summary = RunSummary(report_path=report)
        if not report.exists():
            return summary

        for testcase in ET.parse(report).iter("testcase"):
            status = "passed"
            message = ""
            for child in testcase:
                if child.tag in _STATUS_MAP:
                    status = _STATUS_MAP[child.tag]
                    message = "\n".join(filter(None, [child.get("message") or "", child.text or ""]))
            summary.results.append(
                CaseResult(
                    name=testcase.get("name", ""),
                    status=status,
                    duration=float(testcase.get("time") or 0.0),
                    message=message.strip(),
                )
            )
        return summary