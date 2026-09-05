"""支持 `python -m weave` 入口。"""
from __future__ import annotations

import sys

from weave_agent_sdk.cli import main

if __name__ == "__main__":
    sys.exit(main())
