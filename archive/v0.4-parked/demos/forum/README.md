# demos/forum — agora(14_forum) × weave v0.4 测试脚本

前置：agora 已**全量运行于 weave v0.4**（LLM + Repository 均已迁移，见
`archive/v0.4-parked/docs-v0.4/demo-forum/MIGRATION-REPORT.md`），运行时不再需要 v0.3 归档包。
脚本自动把 `13_weave` 与 `14_forum` 加入 `sys.path`/`PYTHONPATH`，从仓库根或任意目录运行均可。

| 脚本 | 作用 | 命令 | 预期 |
|------|------|------|------|
| `smoke_real_llm.py` | **1 次真实 DeepSeek 调用**（验证接线+网络+Key，读 `14_forum/weave.yaml` 的 key，不回显） | `python demos/forum/smoke_real_llm.py` | `model=deepseek-chat / base_url=.../v1 / reply=… / exit 0` |
| `run_brainstorm_real.ps1` | **真实 LLM 跑完整 brainstorm 接力**（默认 12 轮；Key 仅注入进程环境变量，输出落 `data/forum_demo_latest.log`） | `.\demos\forum\run_brainstorm_real.ps1 [-Topic "…"]` | 状态行 `status=stopped termination=fixed_rounds turns=12` |
| `run_brainstorm_offline.py` | **FakeLLM 跑完整 brainstorm**（不触网、零成本、确定性；验证机制链路 + 补丁不破坏 SDK 路径） | `python demos/forum/run_brainstorm_offline.py [--topic "…"]` | `status=stopped … turns=12`，每轮固定回复 |
| `run_agora_tests.ps1` | **跑 agora 自身测试套件**（回归：补丁不破坏 FakeLLM/Repository/relay） | `.\demos\forum\run_agora_tests.ps1` | pytest 全绿（此前基线 63 passed） |

说明：
- 真实脚本会把演示 DB 写到 `13_weave/data/forum_demo_*.db`（不污染 `14_forum/agora.db`）；
- 离线/回归脚本不需要任何 API Key，可放心反复跑；
- 真实脚本每次约 12 次 DeepSeek 调用，有小额配额消耗。
