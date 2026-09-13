"""最小可跑示例（真实 LLM）：凭证走环境变量 OPENAI_API_KEY 或 DEEPSEEK_API_KEY。"""
from weave.dx.builder import build_agent_from_yaml
from weave.dx.sync import call_sync

agent = build_agent_from_yaml("weave.yaml")


@agent.tool
def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


if __name__ == "__main__":
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        result = call_sync(agent, q)
        print(f"agent> {result.output}")
