"""Test Prompt Composition Schema — FilePart / SwitchPart / TemplatePart + Tools"""

import os
import sys
import tempfile
import unittest

# Ensure weave module is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from weave.prompts.schema import (
    parse_schema, render_schema, resolve_tool_names,
    PromptSchema, ToolRef, FilePart, SwitchPart, SwitchConfig, TemplatePart,
)


class TestPromptSchema(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Sample prompt files
        self._write("system.md", "你是学习教练。你必须提问评估后才点亮节点。")
        self._write("strategy_default.md", "策略: 用户意图优先，兼顾拓扑杠杆和时间。")
        self._write("strategy_topo.md", "策略: 拓扑排序优先。按 depth 升序推荐，先浅后深。")
        self._write("strategy_spaced.md", "策略: 间隔复习。优先推荐 lit 超过 7 天的节点进行复习。")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, content):
        with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as f:
            f.write(content)

    def _render(self, yaml_dict, variables=None):
        schema = parse_schema(yaml_dict)
        return render_schema(schema, variables or {}, base_dir=self.tmp.name)

    # ── FilePart ──────────────────────────────────────────

    def test_single_file(self):
        """单文件 → 直接加载"""
        result = self._render({"composition": [{"file": "system.md"}]})
        self.assertIn("你是学习教练", result)
        self.assertIn("提问评估", result)

    def test_two_files(self):
        """两个文件 → 按 separator 拼接"""
        yaml = {
            "separator": "\n\n---\n\n",
            "composition": [
                {"file": "system.md"},
                {"file": "strategy_default.md"},
            ],
        }
        result = self._render(yaml)
        self.assertIn("你是学习教练", result)
        self.assertIn("---", result)
        self.assertIn("用户意图优先", result)

    # ── SwitchPart ────────────────────────────────────────

    def test_switch_match(self):
        """变量匹配 → 加载对应文件"""
        yaml = {
            "composition": [
                {"file": "system.md"},
                {"switch": {
                    "variable": "strategy",
                    "cases": {"topo": "strategy_topo.md", "spaced": "strategy_spaced.md"},
                    "default": "strategy_default.md",
                }},
            ],
        }
        result = self._render(yaml, {"strategy": "topo"})
        self.assertIn("拓扑排序优先", result)
        self.assertNotIn("用户意图优先", result)

    def test_switch_default(self):
        """变量不匹配 → 使用 default"""
        yaml = {
            "composition": [
                {"file": "system.md"},
                {"switch": {
                    "variable": "strategy",
                    "cases": {"topo": "strategy_topo.md"},
                    "default": "strategy_default.md",
                }},
            ],
        }
        result = self._render(yaml, {"strategy": "unknown"})
        self.assertIn("用户意图优先", result)

    def test_switch_missing_var(self):
        """变量不存在 → 使用 default"""
        yaml = {
            "composition": [
                {"file": "system.md"},
                {"switch": {
                    "variable": "strategy",
                    "cases": {"topo": "strategy_topo.md"},
                    "default": "strategy_default.md",
                }},
            ],
        }
        result = self._render(yaml)
        self.assertIn("用户意图优先", result)

    def test_switch_no_match_no_default(self):
        """变量不匹配且无 default → 静默跳过"""
        yaml = {
            "composition": [
                {"file": "system.md"},
                {"switch": {
                    "variable": "strategy",
                    "cases": {"topo": "strategy_topo.md"},
                    "default": "",
                }},
            ],
        }
        result = self._render(yaml, {"strategy": "spaced"})
        self.assertIn("你是学习教练", result)
        self.assertNotIn("策略", result)  # no strategy text at all

    # ── TemplatePart ──────────────────────────────────────

    def test_template(self):
        """行内模板 → 变量替换"""
        yaml = {
            "composition": [
                {"template": "领域: {{ domain }}。目标: {{ goal }}。"},
            ],
        }
        result = self._render(yaml, {"domain": "推荐系统", "goal": "理解协同过滤"})
        self.assertIn("领域: 推荐系统", result)
        self.assertIn("目标: 理解协同过滤", result)

    # ── Mixed ─────────────────────────────────────────────

    def test_all_three_parts(self):
        """File + Template + Switch 混合"""
        yaml = {
            "separator": "\n\n",
            "composition": [
                {"file": "system.md"},
                {"template": "当前领域: {{ domain }}。"},
                {"switch": {
                    "variable": "strategy",
                    "cases": {"topo": "strategy_topo.md"},
                    "default": "strategy_default.md",
                }},
            ],
        }
        result = self._render(yaml, {"domain": "ML", "strategy": "topo"})
        self.assertIn("你是学习教练", result)
        self.assertIn("当前领域: ML", result)
        self.assertIn("拓扑排序优先", result)

    def test_shorthand_string_is_file(self):
        """简写字符串 → 视为 file"""
        result = self._render({"composition": ["system.md"]})
        self.assertIn("你是学习教练", result)


class TestSchemaTools(unittest.TestCase):
    """Tests for schema tools: declaration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Create tools/ subdir with tool YAML files
        tools_dir = os.path.join(self.tmp.name, "tools")
        os.makedirs(tools_dir)
        self._write_tool("node_add.yaml", "name: node_add\n")
        self._write_tool("edge_add.yaml", "name: edge_add\n")
        self._write_tool("graph_decompose.yaml", "name: graph_decompose\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_tool(self, name, content):
        path = os.path.join(self.tmp.name, "tools", name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def test_parse_tools_with_include(self):
        """解析 tools: 字段 — include 引用"""
        yaml = {
            "composition": [],
            "tools": [
                {"include": "tools/node_add.yaml"},
                {"include": "tools/edge_add.yaml"},
            ],
        }
        schema = parse_schema(yaml)
        self.assertEqual(len(schema.tools), 2)
        self.assertEqual(schema.tools[0].include, "tools/node_add.yaml")
        self.assertEqual(schema.tools[1].include, "tools/edge_add.yaml")

    def test_parse_tools_shorthand_strings(self):
        """简写字符串 → ToolRef"""
        yaml = {
            "composition": [],
            "tools": ["node_add", "edge_add", "graph_decompose"],
        }
        schema = parse_schema(yaml)
        self.assertEqual(len(schema.tools), 3)
        self.assertEqual(schema.tools[0].include, "node_add")
        self.assertEqual(schema.tools[1].include, "edge_add")

    def test_parse_tools_inline_name(self):
        """内联声明 {name: "tool_name"}"""
        yaml = {
            "composition": [],
            "tools": [
                {"name": "node_add", "description": "添加节点"},
            ],
        }
        schema = parse_schema(yaml)
        self.assertEqual(len(schema.tools), 1)
        self.assertEqual(schema.tools[0].include, "node_add")

    def test_parse_no_tools(self):
        """无 tools 字段 → 空列表（向后兼容）"""
        yaml = {"composition": [{"file": "system.md"}]}
        schema = parse_schema(yaml)
        self.assertEqual(schema.tools, [])

    def test_resolve_tool_names_from_files(self):
        """resolve_tool_names — 从工具 YAML 文件读取 name"""
        schema = PromptSchema(tools=[
            ToolRef(include="tools/node_add.yaml"),
            ToolRef(include="tools/edge_add.yaml"),
        ])
        names = resolve_tool_names(schema, base_dir=self.tmp.name)
        self.assertEqual(names, ["node_add", "edge_add"])

    def test_resolve_tool_names_missing_file(self):
        """文件不存在 → 回退为 stem"""
        schema = PromptSchema(tools=[
            ToolRef(include="tools/nonexistent.yaml"),
        ])
        names = resolve_tool_names(schema, base_dir=self.tmp.name)
        self.assertEqual(names, ["nonexistent"])

    def test_resolve_tool_names_empty(self):
        """空 tools → 空列表"""
        schema = PromptSchema(tools=[])
        names = resolve_tool_names(schema, base_dir=self.tmp.name)
        self.assertEqual(names, [])

    def test_resolve_tool_names_no_name_field(self):
        """YAML 存在但无 name 字段 → 回退 stem"""
        self._write_tool("bad_tool.yaml", "description: missing name field\n")
        schema = PromptSchema(tools=[
            ToolRef(include="tools/bad_tool.yaml"),
        ])
        names = resolve_tool_names(schema, base_dir=self.tmp.name)
        self.assertEqual(names, ["bad_tool"])

    def test_end_to_end_yaml_parse_and_resolve(self):
        """端到端: YAML → parse_schema → resolve_tool_names"""
        import yaml
        yaml_text = """
prompt:
  description: "Phase 3"
  composition:
    - file: system.md
  tools:
    - include: tools/node_add.yaml
    - include: tools/edge_add.yaml
    - include: tools/graph_decompose.yaml
"""
        raw = yaml.safe_load(yaml_text)
        schema = parse_schema(raw)

        # Prompt rendering still works
        self.assertEqual(schema.description, "Phase 3")
        self.assertEqual(len(schema.composition), 1)

        # Tool resolution works
        names = resolve_tool_names(schema, base_dir=self.tmp.name)
        self.assertEqual(names, ["node_add", "edge_add", "graph_decompose"])

    def test_backward_compatible_rendering_with_tools(self):
        """带 tools 的 schema 仍然可以正常渲染 Prompt"""
        yaml = {
            "description": "Phase with tools",
            "separator": "\n\n",
            "composition": [
                {"template": "你是学习教练。带有工具声明。"},
            ],
            "tools": [
                {"include": "tools/node_add.yaml"},
            ],
        }
        result = self._render_with_schema(yaml)
        self.assertIn("你是学习教练", result)

    def _render_with_schema(self, yaml_dict, variables=None):
        """与 setUp 中的 _render 相同，但在本 TestCase 中使用"""
        schema = parse_schema(yaml_dict)
        return render_schema(schema, variables or {}, base_dir=self.tmp.name)


if __name__ == "__main__":
    unittest.main()
