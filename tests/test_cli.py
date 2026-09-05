"""脚手架 CLI 测试（weave init）。

验证生成的骨架是「标准等价文件」——weave.yaml 可被 load_config 解析、
agent.py 可编译、prompts/system.md 存在、重复 init 不覆盖已存在文件。
"""
from __future__ import annotations

import py_compile

from weave.cli import init_project
from weave.config import load_config


def test_init_generates_skeleton(tmp_path, capsys):
    target = tmp_path / "my_agent"
    init_project(str(target))

    # 四个文件都生成
    assert (target / "weave.yaml").exists()
    assert (target / "agent.py").exists()
    assert (target / "prompts" / "system.md").exists()
    assert (target / "README.md").exists()

    # weave.yaml 是标准等价文件，能被 load_config 解析
    cfg = load_config(str(target / "weave.yaml"))
    assert cfg.llm.provider == "anthropic"
    assert cfg.loop.type == "iterative"

    # agent.py 语法正确
    py_compile.compile(str(target / "agent.py"), doraise=True)


def test_init_is_idempotent(tmp_path, capsys):
    """重复 init 不覆盖已存在文件。"""
    target = tmp_path / "my_agent"
    init_project(str(target))

    # 手动改一个文件，再 init，应跳过而非覆盖
    (target / "weave.yaml").write_text("# 用户自定义\n", encoding="utf-8")
    init_project(str(target))

    assert (target / "weave.yaml").read_text(encoding="utf-8") == "# 用户自定义\n"
    captured = capsys.readouterr().out
    assert "跳过" in captured


def test_init_rejects_cli_without_command(capsys):
    from weave.cli import main
    # 无子命令时打印 help 并返回非零
    assert main([]) == 1
