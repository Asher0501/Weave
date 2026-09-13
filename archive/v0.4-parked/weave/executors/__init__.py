"""weave.executors — Executor（环境调用）的参考实现（执行引擎域）。

契约见 weave.core.interfaces.Executor：只执行、返回值 JSON 可序列化、
失败抛 ToolError。
"""
from weave.executors.python_exec import Handler, PythonExecutor

__all__ = ["PythonExecutor", "Handler"]
