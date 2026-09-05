"""ScheduledLoop — 定时后台循环。

Cron 定时触发或事件驱动。Phase 3。
失败不阻塞后续 cron；写入 StateMemory 记录。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import signal
import time
from typing import Any

from weave_agent_sdk.loop.base import BaseLoop, call_llm, format_memory_context, persist_user_message
from weave_agent_sdk.types import LoopResult, Message

logger = logging.getLogger(__name__)


class ScheduledLoop(BaseLoop):
    """按 cron 或事件驱动的后台执行。

    配置:
        schedule: "0 */6 * * *" — cron 表达式（Phase 3: 需 cron 解析库）
        schedule: "@on_data_change" — 宿主通过 weave.emit("data_change") 触发

    运行语义:
        - 未配置 schedule（None 或空字符串）时，run() 单次执行（保持兼容）。
        - 配置了 schedule 时，run() 持续运行直到 handle_shutdown() 被调用，
          每次触发执行一次 agent 交互，失败不阻塞后续触发。
        - 事件驱动模式（@on_data_change）启动即执行一次（预热/首次扫描），
          随后等待 data_change 事件驱动后续触发；预热与首个事件触发之间、
          两次事件触发之间均受 loop.event_min_interval（默认 1.0s）最小间隔
          节流，避免突发背靠背执行（review round-2 issue 5）。
    """

    def __init__(self):
        self._shutting_down = False
        self._current_task: asyncio.Task | None = None
        # 当前是否正在执行一次触发（供 handle_shutdown 区分"执行中"与"等待中"）
        self._executing: bool = False

    async def run(self, agent: Any, user_input: str) -> LoopResult:
        schedule = getattr(agent._config.loop, "schedule", None)

        # 未配置 schedule（或配置非字符串，如测试 MagicMock）时：单次执行
        if not isinstance(schedule, str) or not schedule.strip():
            return await self._execute_once(agent, user_input)

        # 配置了 schedule：持续运行直到 shutdown
        self._current_task = asyncio.current_task()
        self._executing = False
        # 复位 shutdown 标志：允许同一实例在 handle_shutdown() 后重新启动常驻
        # 调度。_run_impl 的 _is_running 重入守卫在 finally 中复位（语义上允许
        # shutdown 后再启动）；若不复位，_shutting_down 保持 True，重启的 run()
        # 的 while not self._shutting_down 会立即退出并返回空 LoopResult——用户
        # 的重启调用被静默吞掉（review round-1 issue 4）。
        self._shutting_down = False

        # 事件驱动调度（@on_data_change）在循环外建立常驻订阅：
        # 避免"每次触发后才订阅"导致 _execute_once 执行期间（LLM 调用可能
        # 数秒到数十秒）发出的 data_change 事件因无人订阅而丢失。
        data_stream = None
        if schedule.strip() == "@on_data_change":
            bus = getattr(agent, "__dict__", {}).get("_event_bus")
            if bus is not None:
                data_stream = bus.subscribe("data_change")

        last_result: LoopResult | None = None
        try:
            # 事件驱动模式：启动即执行一次（与第8轮语义一致），
            # 保证"启动即首次扫描/预热"的宿主无需等待首个事件。
            if data_stream is not None:
                if not self._shutting_down:
                    last_result = await self._execute_trigger(agent, user_input)
                    # 预热与首个 data_change 事件之间同样受 event_min_interval
                    # 节流：预热（LLM 调用）可能持续数秒到数十秒，期间宿主 emit
                    # 的 data_change 会积压在队列；若预热结束立即消费首个积压
                    # 事件，会与预热"背靠背"连续执行（review round-2 issue 5）。
                    await self._throttle_between_triggers(agent)

            while not self._shutting_down:
                trigger_input = user_input
                if data_stream is not None:
                    # 事件驱动模式：消费一个 data_change 事件，并将其 payload
                    # 作为本次触发输入（而非复用固定 user_input），使宿主
                    # emit("data_change", {"input": ...}) 能驱动不同的触发内容。
                    try:
                        async for _event in data_stream:
                            event_data = _event.data or {}
                            trigger_input = event_data.get("input", user_input)
                            break
                    except asyncio.CancelledError:
                        raise
                else:
                    # cron / 间隔模式：等待下一次触发时机（可被 handle_shutdown 打断）
                    try:
                        await self._wait_for_next_run(agent, schedule)
                    except asyncio.CancelledError:
                        raise

                if self._shutting_down:
                    break

                last_result = await self._execute_trigger(agent, trigger_input)

                if self._shutting_down:
                    break

                # 事件驱动模式：两次触发间最小间隔节流（合并突发），
                # 防止 data_change 突发的背靠背执行放大成本 / 触发限流。
                if data_stream is not None:
                    await self._throttle_between_triggers(agent)
        except asyncio.CancelledError:
            # handle_shutdown() 取消当前任务时的正常退出路径
            pass
        finally:
            self._current_task = None
            self._executing = False
            if data_stream is not None:
                try:
                    await data_stream.aclose()
                except Exception as e:
                    logger.warning(
                        "ScheduledLoop: failed to close data_change subscription: %s", e
                    )

        if last_result is not None:
            return last_result
        return LoopResult(
            output="",
            elapsed_ms=0,
            iterations=0,
            memory_updated={},
        )

    async def _throttle_between_triggers(self, agent: Any) -> None:
        """事件驱动模式：两次触发间的最小间隔节流（合并突发）。

        防止 data_change 突发时"预热 / 事件触发"背靠背执行放大成本 / 触发限流。
        间隔从 loop.event_min_interval 读取（默认 1.0s）；非数值 / 负数配置回退
        默认值（review round-2 issue 5）。
        """
        min_interval = getattr(agent._config.loop, "event_min_interval", 1.0)
        if not isinstance(min_interval, (int, float)) or min_interval < 0:
            min_interval = 1.0
        if min_interval > 0:
            try:
                await asyncio.sleep(min_interval)
            except asyncio.CancelledError:
                raise

    async def _execute_trigger(self, agent: Any, trigger_input: str) -> LoopResult | None:
        """在共享锁内执行一次触发。

        - 锁只覆盖单次触发，避免整个常驻周期持锁饿死同事件循环上的
          其他 arun()/stream()/run() 操作；
        - 触发执行期间与并发操作串行化，避免竞争共享实例状态
          （_system_prompt / active_scopes / _memory_writes 等）。
        - 单个触发失败不终止整个调度器（docstring 承诺"失败不阻塞后续
          cron"）：记录告警，返回 None 让调用方继续等待下一次触发。
        """
        try:
            loop = asyncio.get_running_loop()
            locks = agent.__dict__.setdefault("_run_locks", {})
            lock = locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                locks[loop] = lock
            async with lock:
                return await self._execute_once(agent, trigger_input)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "ScheduledLoop: trigger execution failed, "
                "will retry on next schedule: %s", e
            )
            return None

    async def _execute_once(self, agent: Any, user_input: str) -> LoopResult:
        """执行一次迭代。"""
        t_start = time.perf_counter()
        self._executing = True
        await self.on_start(agent, user_input)

        # 重置本次触发的 memory 写入计数：常驻 scheduled 在同一次 _run_impl_inner
        # 内多次触发，若不重置，before_think 的排除逻辑会把前几次触发的历史也
        # 当作"本次写入"剔除，导致跨触发记忆被清空（"能记住"承诺失效）。
        # 计数由 persist_user_message / after_think 钩子在本触发内重新累计。
        agent.__dict__.pop("_memory_writes", None)

        try:
            # 注入 memory context
            memory_ctx = await self.before_think(agent, user_input)

            # 构建 messages
            messages = [
                Message(role="system", content=agent._system_prompt),
                Message(role="user", content=user_input),
            ]
            if memory_ctx:
                ctx_text = format_memory_context(memory_ctx)
                messages[0].content += "\n\n" + ctx_text

            # LLM 调用（流式模式下逐 token emit 事件）
            response = await call_llm(agent, messages, None)

            # 延迟提交 user 消息：仅在成功获得 assistant 回复后落盘，避免触发被
            # 取消/超时打断（handle_shutdown 强制取消 / 流式空闲超时 kill）时
            # 遗留"有 user 无 assistant"的悬空消息污染后续记忆注入（与 iterative
            # 的延迟提交模式一致，review round-14 issue 2）。
            await persist_user_message(agent, user_input)

            # 将 assistant response 持久化到 stream Memory（after_think）
            await self.after_think(agent, response)

            t_end = time.perf_counter()

            # 更新公开状态 status().last_run：常驻 scheduled 期间 _run_impl_inner 的
            # finally 仅在 shutdown 时执行，若不在此更新，status().last_run 恒为初始值，
            # 与 StateMemory 实际记录的 last_run_at 脱节。
            agent._last_run = time.time()

            # 记录最后运行结果到 StateMemory。
            # 注意：last_run_at 必须使用墙上时钟（time.time()），而非单调钟
            # （time.perf_counter()）。perf_counter 无墙钟语义（epoch），跨进程/
            # 重启后无法与 time.time() 比较，与 agent._last_run 也不一致
            # （review round-15 issue 3）。
            # 仅当激活了 state namespace 时写入：未配置任何 state scope 时不写
            # "default:session:state" 这类不属于任何激活 scope 的幻影 namespace
            # ——该幻影行不被 before_think 的 get_all(state_ns) 读到，却会出现在
            # stats/namespace_stats 中，污染统计且与配置模型脱节
            # （review round-4 issue 5）。
            state_writes: dict[str, int] = {}
            try:
                namespaces = agent._memory.get_namespaces("state")
                if namespaces:
                    session_ns = namespaces[0]
                    agent._memory.state.set("last_run_at", time.time(), session_ns)
                    agent._memory.state.set("last_run_error", None, session_ns)
                    state_writes[session_ns] = 2
            except FileNotFoundError:
                # namespace 对应的后端文件不存在是正常情况（首次运行或配置变更）
                pass
            except Exception as e:
                # 非关键路径：写入失败不影响主流程，但记录告警便于排查
                logger.warning("Failed to save scheduled run state: %s", e)

            # 从 after_think 记录的写入计数汇总 memory_updated（真实写入，非虚构值）
            memory_updated: dict[str, dict[str, int]] = {}
            memory_writes = getattr(agent, "__dict__", {}).get("_memory_writes") or {}
            if memory_writes.get("stream"):
                memory_updated["stream"] = memory_writes["stream"]
            if memory_writes.get("state"):
                memory_updated["state"] = memory_writes["state"]
            # 本方法直接写入的 state（last_run_at / last_run_error）也计入，
            # 保证 memory_updated["state"] 反映实际发生的 state 写入
            if state_writes:
                merged_state = dict(memory_writes.get("state") or {})
                for ns, count in state_writes.items():
                    merged_state[ns] = merged_state.get(ns, 0) + count
                memory_updated["state"] = merged_state

            result = LoopResult(
                output=response.content,
                elapsed_ms=int((t_end - t_start) * 1000),
                iterations=1,
                memory_updated=memory_updated,
            )

            await self.on_end(agent, result)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 触发失败：把错误写入 StateMemory 的 last_run_error，避免状态记录
            # 始终显示成功、故障排查失去依据（review round-15 issue 4）。
            # 与成功路径一致：仅当激活了 state namespace 时写入，不写幻影
            # namespace（review round-4 issue 5）。写入失败不影响主流程，
            # 仍向上传播异常由 _execute_trigger 处理。
            try:
                namespaces = agent._memory.get_namespaces("state")
                if namespaces:
                    session_ns = namespaces[0]
                    agent._memory.state.set(
                        "last_run_error",
                        f"{type(e).__name__}: {e}",
                        session_ns,
                    )
            except FileNotFoundError:
                # namespace 对应的后端文件不存在是正常情况（首次运行或配置变更）
                pass
            except Exception as write_err:
                logger.warning(
                    "Failed to record scheduled run error to state: %s", write_err
                )
            raise
        finally:
            self._executing = False

    async def _wait_for_next_run(self, agent: Any, schedule: str) -> None:
        """等待下一次触发时机（可被 handle_shutdown() 打断）。

        支持两种 schedule:
        - cron 表达式（5 段）: 按下一次匹配时间等待
        - 纯数字: 视为间隔秒数

        事件驱动（@on_data_change）已由 run() 的循环外常驻订阅实现，这里不再
        处理该值——若走到此处（如常驻订阅缺失的异常场景），"@on_data_change"
        无法解析为 cron / 数值，回退 60s 轮询间隔并记录告警，避免无节流紧循环
        （review round-2 issue 5：移除主路径死代码分支）。
        """
        s = schedule.strip()

        # 数值间隔（秒）
        if s.isdigit():
            interval = float(s)
            if interval <= 0:
                # 配置笔误（如 "0"）会退化为 0 间隔的忙循环——每次触发
                # 真实 LLM 调用且无任何 sleep，打爆 API 配额。回退默认
                # 间隔并记录告警，避免无节流紧循环。
                logger.warning(
                    "ScheduledLoop: invalid interval '%s' (must be > 0 seconds), "
                    "defaulting to 60s interval",
                    s,
                )
                interval = 60.0
        else:
            next_ts = _next_cron_timestamp(s, time.time())
            if next_ts is None:
                logger.warning(
                    "ScheduledLoop: cannot parse schedule '%s', defaulting to 60s interval",
                    schedule,
                )
                interval = 60.0
            else:
                interval = max(next_ts - time.time(), 0.0)

        # 分片等待，保证 handle_shutdown() 能及时中断
        deadline = time.time() + interval
        while not self._shutting_down:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 1.0))

    def handle_shutdown(self) -> None:
        """优雅关闭：完成当前迭代，超时 30s 强制终止。

        线程安全：cancel() 通过事件循环调度，避免从其他线程（REST 关闭接口、
        信号处理）直接操作 asyncio.Task 导致线程安全问题。

        策略按状态区分：
        - 当前迭代执行中：宽限 30s 让其自然完成，超时强制取消；
        - 等待中（事件驱动阻塞于 queue.get() / cron 睡眠）：事件驱动的
          queue.get() 无法被标志位唤醒，只能靠 cancel 打断，立即取消。
        """
        self._shutting_down = True
        task = self._current_task
        if not task or task.done():
            return
        try:
            loop = task.get_loop()

            if self._executing:
                # 当前迭代执行中：宽限 30s 让当前迭代自然完成，
                # 超时后强制取消。注意：call_later 本身非线程安全——若
                # handle_shutdown() 从非事件循环线程调用（REST 关闭接口 /
                # 信号处理在 worker 线程），需先经 call_soon_threadsafe 调度
                # 到事件循环线程，再在循环内 call_later，与下方立即取消分支
                # 的线程安全语义保持一致。
                def _force_cancel() -> None:
                    if self._current_task and not self._current_task.done():
                        self._current_task.cancel()

                loop.call_soon_threadsafe(lambda: loop.call_later(30.0, _force_cancel))
            else:
                # 等待中：立即取消以打断阻塞（事件驱动 queue.get() / cron 睡眠）
                loop.call_soon_threadsafe(self._current_task.cancel)
        except Exception as e:
            logger.warning("Error during scheduled loop shutdown: %s", e)


# ── Helpers ──────────────────────────────────────────────

def _parse_cron_field(field: str, lo: int, hi: int) -> set[int]:
    """解析单个 cron 字段，支持 *, 列表, 范围, 步长。"""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(lo, hi + 1))
        elif "/" in part:
            base, step = part.split("/")
            step = int(step)
            if base == "*":
                values.update(range(lo, hi + 1, step))
            else:
                a, b = (int(x) for x in base.split("-"))
                values.update(range(a, b + 1, step))
        elif "-" in part:
            a, b = (int(x) for x in part.split("-"))
            values.update(range(a, b + 1))
        else:
            values.add(int(part))
    return {v for v in values if lo <= v <= hi}


def _next_cron_timestamp(schedule: str, now: float) -> float | None:
    """计算 cron 表达式下一次匹配的时间戳（简化实现）。

    支持标准 5 段 cron: 分 时 日 月 周(0-6, 0=周日)。
    DOM 与 DOW 采用 AND 语义（简化，未实现标准 cron 的 OR 语义），
    满足常规整点/周期调度场景。
    """
    fields = schedule.split()
    if len(fields) != 5:
        return None
    try:
        minutes = _parse_cron_field(fields[0], 0, 59)
        hours = _parse_cron_field(fields[1], 0, 23)
        doms = _parse_cron_field(fields[2], 1, 31)
        months = _parse_cron_field(fields[3], 1, 12)
        dows = _parse_cron_field(fields[4], 0, 6)
    except (ValueError, TypeError):
        return None

    cur = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
    for _ in range(60 * 24 * 366):  # 最多扫描一年
        cur = cur + _dt.timedelta(minutes=1)
        if cur.month not in months:
            continue
        if cur.day not in doms:
            continue
        # cron DOW 语义: 0=Sunday ... 6=Saturday；而 Python datetime.weekday()
        # 为 Monday=0 ... Sunday=6。直接用 weekday() 与 cron 解析出的 dows 比较
        # 会整体偏移一天（周日/周六永不触发，周一至周六错位）。先将 weekday()
        # 转换为 cron DOW 表示再比较。
        cron_dow = (cur.weekday() + 1) % 7
        if cron_dow not in dows:
            continue
        if cur.hour not in hours:
            continue
        if cur.minute not in minutes:
            continue
        return cur.timestamp()
    return None
