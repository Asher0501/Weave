"""14_forum（agora）× weave v0.4 真实 LLM demo 运行器。

用法（工作目录 = 14_forum）：
    python <13_weave>/demos/forum/run_agora_demo.py run --config scenarios/brainstorm.yaml \
        --topic "..." --db <workspace db>
"""
import sys

from agora.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
