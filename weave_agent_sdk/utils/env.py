"""环境变量工具函数。

提供从 ~/.claude/settings.json 加载环境变量的共享实现，
避免在多个模块中重复实现且质量不一致。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_claude_env() -> None:
    """从 ~/.claude/settings.json 读取 env 并注入 os.environ。

    不覆盖已有环境变量。参考 Nexus llm_sdk.py 的 _load_settings_env()。

    异常处理策略（分类处理，不静默吞异常）：
      - FileNotFoundError: settings.json 不存在是正常情况，静默通过
      - JSONDecodeError: 记录警告日志
      - PermissionError: 记录警告日志
      - 其他异常: 记录警告日志（兜底）
    """
    import os

    settings_path = Path.home() / ".claude" / "settings.json"
    try:
        if settings_path.exists():
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            claude_env = data.get("env", {})
            if isinstance(claude_env, dict):
                for k, v in claude_env.items():
                    if k not in os.environ and isinstance(v, str):
                        os.environ[str(k)] = v
    except FileNotFoundError:
        # settings.json 不存在是正常情况，无需告警
        pass
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse ~/.claude/settings.json: %s", e)
    except PermissionError as e:
        logger.warning("Permission denied reading ~/.claude/settings.json: %s", e)
    except Exception as e:
        logger.warning("Unexpected error loading ~/.claude/settings.json: %s", e)
