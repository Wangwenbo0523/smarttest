"""命令行入口单元测试。

CLI 是「这个项目是不是一个真工具」的门面：装了包之后 `smarttest run --target users`
就能跑，参数与仓库里的脚本完全一致。这里钉住分发逻辑与输出。
"""
from __future__ import annotations

from smarttest import __version__
from smarttest.cli import main


def test_no_arguments_prints_usage(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "SmartTest" in out
    assert "smarttest run" in out


def test_help_flag_prints_usage(capsys):
    assert main(["--help"]) == 0
    assert "smarttest targets" in capsys.readouterr().out


def test_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_targets_lists_every_registered_target(capsys):
    assert main(["targets"]) == 0
    out = capsys.readouterr().out
    for expected in ("orders", "users", "articles", "8123", "8124", "8125"):
        assert expected in out
    assert "共 3 个靶场" in out


def test_unknown_command_returns_two(capsys):
    assert main(["nope"]) == 2
    out = capsys.readouterr().out
    assert "未知子命令" in out
    assert "用法" in out


def test_run_delegates_the_remaining_arguments(monkeypatch):
    captured: dict = {}

    def fake_run(argv):
        captured["argv"] = argv
        return 7

    monkeypatch.setattr("smarttest.pipeline.main", fake_run)
    assert main(["run", "--target", "users", "--target-mode", "fixed"]) == 7
    assert captured["argv"] == ["--target", "users", "--target-mode", "fixed"]


def test_eval_delegates_the_remaining_arguments(monkeypatch):
    captured: dict = {}

    def fake_eval(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr("smarttest.evaluation.main", fake_eval)
    assert main(["eval", "--target", "articles", "--min-recall", "100"]) == 0
    assert captured["argv"] == ["--target", "articles", "--min-recall", "100"]
