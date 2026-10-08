"""本地环境变量加载的单元测试。

`.env.local` 是模型密钥的唯一来源（该文件不会进仓库）。这里的规则很简单，
但错一条就会很别扭：要么密钥没生效，要么命令行显式传的值被文件里的旧值盖掉。

注意：`load_local_env` 是直接写 `os.environ` 的，测完必须还原 ——
否则一个用例留下的值会污染同一次会话里的其他用例。
"""
from __future__ import annotations

import os

import pytest

from smarttest.localenv import load_local_env


@pytest.fixture(autouse=True)
def restore_environ():
    """快照并还原 os.environ：这一组用例改的是进程级环境变量。"""
    before = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


def write_env(root, text: str) -> None:
    (root / ".env.local").write_text(text, encoding="utf-8")


def test_missing_file_is_a_noop(tmp_path):
    load_local_env(tmp_path)
    assert "SMARTTEST_LLM_API_KEY" not in os.environ


def test_reads_keys_into_the_environment(tmp_path):
    write_env(
        tmp_path,
        "# 注释行\n"
        "\n"
        "SMARTTEST_LLM_API_KEY=sk-from-file\n"
        "SMARTTEST_LLM_MODEL = deepseek-chat\n",
    )

    load_local_env(tmp_path)

    assert os.environ["SMARTTEST_LLM_API_KEY"] == "sk-from-file"
    assert os.environ["SMARTTEST_LLM_MODEL"] == "deepseek-chat"  # 键与值都 strip


def test_existing_environment_wins(tmp_path):
    # 用 setdefault 而不是覆盖：命令行显式传入的值优先级更高
    os.environ["SMARTTEST_LLM_API_KEY"] = "sk-from-shell"
    write_env(tmp_path, "SMARTTEST_LLM_API_KEY=sk-from-file\n")

    load_local_env(tmp_path)

    assert os.environ["SMARTTEST_LLM_API_KEY"] == "sk-from-shell"


def test_value_may_contain_equals_signs(tmp_path):
    write_env(tmp_path, "SMARTTEST_LLM_BASE_URL=https://x.example/v1?a=b\n")
    load_local_env(tmp_path)

    assert os.environ["SMARTTEST_LLM_BASE_URL"] == "https://x.example/v1?a=b"


def test_lines_without_equals_are_skipped(tmp_path):
    write_env(tmp_path, "这不是一个配置行\nSMARTTEST_LLM_MODEL=m\n")
    load_local_env(tmp_path)

    assert os.environ["SMARTTEST_LLM_MODEL"] == "m"
