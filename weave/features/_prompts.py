"""features 默认 Prompt 模板的加载辅助。

features 的默认 Prompt 从 prompts/features/*.md 加载（R2：代码中绝不硬编码
Prompt）。模板目录路径集中在此单一模块定义，避免在多个 feature 模块中重复
硬编码（review round-3 issue 3）；文件缺失时给出友好引导，而非裸
FileNotFoundError，宿主开启对应 feature 后按引导创建模板即可。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from weave.prompts.loader import interpolate, load_prompt_raw

# Feature 默认 Prompt 模板目录（宿主侧，与 weave/ 包分离；按项目惯例为
# CWD 下的 prompts/features/，与 PromptRegistry 的 base_dir="prompts" 一致）。
FEATURE_PROMPTS_DIR = Path("prompts") / "features"


def _load(name: str) -> str:
    """加载 prompts/features/{name}.md 原始模板；缺失时给出友好引导错误。"""
    path = FEATURE_PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"Feature prompt file not found: {path}. Please create "
            f"'{path}' (copy the template from the weave repository's "
            f"prompts/features/) before enabling the corresponding feature."
        )
    return load_prompt_raw(path)


def feature_prompt(name: str, **context: Any) -> str:
    """加载并渲染 prompts/features/{name}.md 模板。"""
    return interpolate(_load(name), context)


def feature_prompt_raw(name: str) -> str:
    """加载 prompts/features/{name}.md 原始模板（不做变量替换）。"""
    return _load(name)
