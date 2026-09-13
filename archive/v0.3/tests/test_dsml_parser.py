"""Test DSML Parser — DeepSeek V4 Pro tool call text parsing."""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from weave_agent_sdk.llm.dsml_parser import has_dsml, parse_dsml_tool_calls
from weave_agent_sdk.types import ToolCall


class TestDSMLParser(unittest.TestCase):

    # ── has_dsml ────────────────────────────────────────────

    def test_has_dsml_detects_block(self):
        text = "前缀 <｜DSML｜tool_calls><｜DSML｜invoke name=\"search\">" \
               "<｜DSML｜parameter name=\"q\" string=\"true\">hello</｜DSML｜parameter>" \
               "</｜DSML｜invoke></｜DSML｜tool_calls> 后缀"
        self.assertTrue(has_dsml(text))

    def test_has_dsml_no_marker(self):
        self.assertFalse(has_dsml("普通文本，没有 DSML"))

    def test_has_dsml_ascii_pipe(self):
        """ASCII | 也应该能匹配"""
        text = "前缀 <|DSML|tool_calls><|DSML|invoke name=\"search\">" \
               "<|DSML|parameter name=\"q\" string=\"true\">hello</|DSML|parameter>" \
               "</|DSML|invoke></|DSML|tool_calls> 后缀"
        self.assertTrue(has_dsml(text))

    # ── parse_dsml_tool_calls ───────────────────────────────

    def test_single_tool_single_string_param(self):
        text = "我来搜索。<｜DSML｜tool_calls>\n" \
               "<｜DSML｜invoke name=\"search\">\n" \
               "<｜DSML｜parameter name=\"query\" string=\"true\">Python教程</｜DSML｜parameter>\n" \
               "</｜DSML｜invoke>\n" \
               "</｜DSML｜tool_calls>"

        tool_calls, cleaned = parse_dsml_tool_calls(text)

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].name, "search")
        self.assertEqual(tool_calls[0].arguments, {"query": "Python教程"})
        self.assertNotIn("DSML", cleaned)
        self.assertIn("我来搜索。", cleaned)

    def test_single_tool_multiple_params(self):
        text = "<｜DSML｜tool_calls>\n" \
               "<｜DSML｜invoke name=\"write_file\">\n" \
               "<｜DSML｜parameter name=\"path\" string=\"true\">/tmp/test.txt</｜DSML｜parameter>\n" \
               "<｜DSML｜parameter name=\"content\" string=\"true\">hello world</｜DSML｜parameter>\n" \
               "</｜DSML｜invoke>\n" \
               "</｜DSML｜tool_calls>"

        tool_calls, cleaned = parse_dsml_tool_calls(text)

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].name, "write_file")
        self.assertEqual(tool_calls[0].arguments, {
            "path": "/tmp/test.txt",
            "content": "hello world",
        })
        self.assertEqual(cleaned, "")

    def test_json_param(self):
        """string="false" 时参数值应被 JSON 解析。"""
        text = "<｜DSML｜tool_calls>\n" \
               "<｜DSML｜invoke name=\"add_nodes\">\n" \
               "<｜DSML｜parameter name=\"nodes\" string=\"false\">" \
               '[{"name": "A", "type": "concept"}]' \
               "</｜DSML｜parameter>\n" \
               "</｜DSML｜invoke>\n" \
               "</｜DSML｜tool_calls>"

        tool_calls, cleaned = parse_dsml_tool_calls(text)

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].arguments["nodes"],
                         [{"name": "A", "type": "concept"}])

    def test_multiple_invocations(self):
        text = "<｜DSML｜tool_calls>\n" \
               "<｜DSML｜invoke name=\"list_dir\">\n" \
               "<｜DSML｜parameter name=\"path\" string=\"true\">.</｜DSML｜parameter>\n" \
               "</｜DSML｜invoke>\n" \
               "<｜DSML｜invoke name=\"search\">\n" \
               "<｜DSML｜parameter name=\"query\" string=\"true\">README</｜DSML｜parameter>\n" \
               "</｜DSML｜invoke>\n" \
               "</｜DSML｜tool_calls>"

        tool_calls, cleaned = parse_dsml_tool_calls(text)

        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(tool_calls[0].name, "list_dir")
        self.assertEqual(tool_calls[1].name, "search")

    def test_no_dsml_returns_unchanged(self):
        text = "普通回复，没有工具调用。"
        tool_calls, cleaned = parse_dsml_tool_calls(text)

        self.assertEqual(tool_calls, [])
        self.assertEqual(cleaned, text)

    def test_dsml_stripped_from_surrounding_text(self):
        text = "好的，我先搜索一下。\n\n" \
               "<｜DSML｜tool_calls>\n" \
               "<｜DSML｜invoke name=\"search\">\n" \
               "<｜DSML｜parameter name=\"q\" string=\"true\">test</｜DSML｜parameter>\n" \
               "</｜DSML｜invoke>\n" \
               "</｜DSML｜tool_calls>\n\n" \
               "搜索完成后我会继续分析。"

        tool_calls, cleaned = parse_dsml_tool_calls(text)

        self.assertEqual(len(tool_calls), 1)
        self.assertIn("好的，我先搜索一下。", cleaned)
        self.assertIn("搜索完成后我会继续分析。", cleaned)
        self.assertNotIn("DSML", cleaned)

    def test_toolcall_has_valid_id(self):
        text = "<｜DSML｜tool_calls>\n" \
               "<｜DSML｜invoke name=\"search\">\n" \
               "<｜DSML｜parameter name=\"q\" string=\"true\">test</｜DSML｜parameter>\n" \
               "</｜DSML｜invoke>\n" \
               "</｜DSML｜tool_calls>"

        tool_calls, _ = parse_dsml_tool_calls(text)

        self.assertTrue(tool_calls[0].id.startswith("dsml_"))
        self.assertEqual(len(tool_calls[0].id), 17)  # "dsml_" + 12 hex

    def test_ascii_pipe_format(self):
        """ASCII | 格式（非全角竖线）"""
        text = "好的。<|DSML|tool_calls>\n" \
               "<|DSML|invoke name=\"search\">\n" \
               "<|DSML|parameter name=\"q\" string=\"true\">hello</|DSML|parameter>\n" \
               "</|DSML|invoke>\n" \
               "</|DSML|tool_calls>"

        tool_calls, cleaned = parse_dsml_tool_calls(text)

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].name, "search")
        self.assertNotIn("DSML", cleaned)

    def test_json_param_malformed_fallback(self):
        """JSON 解析失败时退回原始字符串。"""
        text = "<｜DSML｜tool_calls>\n" \
               "<｜DSML｜invoke name=\"bad_tool\">\n" \
               "<｜DSML｜parameter name=\"data\" string=\"false\">not valid json{{{</｜DSML｜parameter>\n" \
               "</｜DSML｜invoke>\n" \
               "</｜DSML｜tool_calls>"

        tool_calls, _ = parse_dsml_tool_calls(text)

        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].arguments["data"], "not valid json{{{")


if __name__ == "__main__":
    unittest.main()
