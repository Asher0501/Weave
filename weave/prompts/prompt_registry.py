"""命名 Prompt 注册表。

管理命名 prompt，支持注册、查询、列出。
"""
from __future__ import annotations

from pathlib import Path

from weave.prompts.loader import load_prompt, load_prompt_raw


class PromptRegistry:
    """命名 Prompt 注册表。

    用法:
        registry = PromptRegistry(base_dir="./prompts", config=weave_config)
        system_prompt = registry.get("system")  # 加载 prompts/system.md
    """

    def __init__(self, base_dir: str | Path = "./prompts", config: dict | None = None):
        self._base_dir = Path(base_dir)
        self._config = config or {}
        self._cache: dict[str, str] = {}

    def register(self, name: str, content: str) -> None:
        """注册一个命名 prompt（字符串，非文件）。"""
        self._cache[name] = content

    def get(self, name: str, context: dict | None = None) -> str:
        """获取命名 prompt，优先从缓存取，否则从文件加载。

        Args:
            name: prompt 名称（对应 prompts/{name}.md 文件）
            context: 运行时变量

        Returns:
            渲染后的 prompt 文本
        """
        # 优先缓存
        if name in self._cache:
            from weave.prompts.loader import interpolate
            return interpolate(self._cache[name], context or {}, self._config)

        # 从文件加载（优先 Schema，其次普通 .md）
        file_md = self._base_dir / f"{name}.md"
        file_schema_yaml = self._base_dir / f"{name}.schema.yaml"
        file_schema_yml = self._base_dir / f"{name}.schema.yml"
        file_schema_json = self._base_dir / f"{name}.schema.json"

        # 检查 Schema 文件
        for schema_path in (file_schema_yaml, file_schema_yml, file_schema_json):
            if schema_path.exists():
                from weave.prompts.loader import load_prompt_schema
                return load_prompt_schema(schema_path, context or {}, self._config)

        # 回退到普通 .md 文件
        if file_md.exists():
            return load_prompt(file_md, context, self._config)

        raise FileNotFoundError(
            f"Prompt '{name}' not registered and no file found: {file_md} or {file_schema_yaml}"
        )

    def get_raw(self, name: str) -> str:
        """获取未渲染的 prompt 原文。"""
        if name in self._cache:
            return self._cache[name]

        file_path = self._base_dir / f"{name}.md"
        if file_path.exists():
            return load_prompt_raw(file_path)

        raise FileNotFoundError(f"Prompt '{name}' not found")

    def list(self) -> list[str]:
        """列出所有可用的 prompt 名称（注册的 + 文件中的）。"""
        names = set(self._cache.keys())
        if self._base_dir.exists():
            for f in self._base_dir.glob("*.md"):
                names.add(f.stem)
            for pattern in ("*.schema.yaml", "*.schema.yml", "*.schema.json"):
                for f in self._base_dir.glob(pattern):
                    # 文件名 "coach.schema.yaml" → name "coach"
                    name = f.name.replace(".schema.yaml", "").replace(".schema.yml", "").replace(".schema.json", "")
                    names.add(name)
        return sorted(names)

    def invalidate(self, name: str) -> None:
        """清除缓存中的指定 prompt。"""
        self._cache.pop(name, None)
