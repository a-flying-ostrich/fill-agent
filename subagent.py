"""
子Agent模块 (对应 learn-claude-code s06)

主Agent遇到复杂子任务时，派生子Agent处理：
- 子Agent有独立的 messages 列表（上下文隔离）
- 子Agent有自己的 Agent Loop（简化版，无钩子、无 nag、无 stop_nudge）
- 工具集更小（不含 task 工具，防止递归派生）
- 30 轮安全限制
- 完成后只返回最终文本摘要，中间历史丢弃

关键价值：保护主Agent的上下文不被子任务细节污染

适合填表场景：
- 每个表单独查询时，主Agent派生子Agent专门处理SQL编写和调试
- 子Agent返回查询成功摘要，主Agent上下文不被SQL调试细节污染
- 主Agent只需要知道"子任务完成了，结果如何"，不需要知道中间调了几次SQL、报了几次错

与 FillAgent 的区别（简化设计）：
| 特性         | FillAgent        | Subagent         |
|-------------|------------------|------------------|
| Hooks       | 4 类钩子          | 无（简化）        |
| Permission  | 三道门管道         | 基础SQL安全检查    |
| TodoWrite   | nag 提醒          | 无               |
| Memory      | 跨会话记忆         | 无（隔离）        |
| Skill       | 按需加载           | 无（隔离）        |
| task 工具   | 有                | 无（防递归）       |
| 最大轮次    | 50               | 30               |

OpenAI 格式适配：
- tool result 是 {"role": "tool", "tool_call_id": "...", "content": "..."} 独立消息
- assistant 消息 content 是字符串（不是 Anthropic 的 block 列表）
"""

import json
from typing import List, Dict, Optional
from context_compact import ContextCompactor


# 子Agent的专用系统提示词（比主Agent简单，聚焦于具体任务）
SUBAGENT_SYSTEM_PROMPT = """你是一个专门处理子任务的助手。主Agent分配给你一个具体的任务，你需要独立完成它。

## 可用工具
- import_excel(file_path): 将Excel文件导入数据库
- list_tables(): 查看数据库中所有表及其字段信息
- execute_sql(sql): 执行SQL SELECT查询并返回结果
- export_result(sql, file_path): 将SQL查询结果导出为CSV文件
- ask_user(question): 向用户提问

## 工作原则
1. 你只负责完成分配给你的具体任务，不要超出任务范围
2. 每次只调用一个工具，等结果返回后再决定下一步
3. SQL只允许SELECT语句，不能修改或删除数据
4. 中文表名和字段名用反引号(`)包裹
5. 如果遇到问题无法解决，简要说明原因
6. 完成任务后，用一段话总结你的操作和结果，包括：执行了什么操作、结果如何、是否有需要注意的事项
7. 不要在文本回复中写SQL语句，SQL只能通过execute_sql工具的sql参数传递
8. 文字说明保持极简（1-2句话），把具体内容交给工具参数
"""


class Subagent:
    """
    子Agent：拥有独立的对话上下文和简化的 Agent Loop。

    使用方式（由主Agent的 task 工具调用）：
        sub = Subagent(llm_client, tool_manager)
        result = sub.run("查询学生信息表中所有获奖学生的姓名和学号")

    子Agent完成后：
    - 只返回最终文本摘要（run() 的返回值）
    - 中间对话历史（messages）随 Subagent 实例销毁而丢弃
    - 主Agent的 messages 不受影响（上下文隔离）
    """

    def __init__(self, llm_client, tool_manager, max_iterations: int = 30):
        """
        初始化子Agent。

        Args:
            llm_client:    LLMClient 实例（与主Agent共享同一个 API 连接）
            tool_manager:  ToolManager 实例（已配置为受限工具集，不含 task 工具）
            max_iterations: 最大迭代次数（默认 30，防止无限循环）
        """
        self.llm = llm_client
        self.tools = tool_manager
        self.max_iterations = max_iterations

        # 子Agent也有上下文压缩器（子任务也可能产生长对话）
        # 阈值比主Agent更宽松（子Agent寿命短，不太容易超限）
        self.compactor = ContextCompactor(
            max_messages=40,
            max_tool_result_chars=20000,
        )

    def run(self, task_description: str) -> str:
        """
        运行子Agent，执行分配的任务，返回最终文本摘要。

        简化的 Agent Loop（与 s06 一致）：
        1. 创建全新的 messages 列表（与主Agent完全隔离）
        2. 循环：调 LLM → 执行工具 → 结果回传 → 继续
        3. LLM 返回纯文本（无 tool_calls）→ 任务完成，返回文本
        4. 达到最大轮次 → 返回超时提示

        与 FillAgent.run() 的区别：
        - 无 Hooks（不需要权限钩子、日志钩子等，简化）
        - 无 nag 提醒（子任务通常简单，不需要任务清单）
        - 无 stop_nudge（子任务完成就退出，不需要强制工具调用）
        - 无 memory 提取（子Agent不记忆，保持隔离）
        - 有 SQL 安全检查（基础安全，直接在循环中检查）
        - 有上下文压缩（子任务也可能产生长对话）

        Args:
            task_description: 主Agent分配的子任务描述

        Returns:
            子Agent的最终文本摘要（主Agent将其作为 tool_result 处理）
        """
        # ---- 创建全新的、独立的 messages 列表 ----
        # 与主Agent的 self.messages 完全隔离，子任务的所有对话都在这个列表里
        # 子Agent完成后，这个列表随实例销毁，不回传给主Agent
        messages: List[Dict] = [
            {"role": "system", "content": SUBAGENT_SYSTEM_PROMPT},
            {"role": "user", "content": task_description},
        ]

        print(f"\n[Subagent] 开始执行子任务: {task_description[:80]}...")

        for iteration in range(self.max_iterations):
            # ---- 上下文压缩（与主Agent相同的压缩管道）----
            llm_messages = self.compactor.compact(messages, self.llm)

            # ---- 调用 LLM ----
            try:
                response = self.llm.chat(llm_messages, self.tools.schemas, "auto")
            except Exception as e:
                error_msg = str(e).lower()
                if "context" in error_msg or "token" in error_msg or "length" in error_msg:
                    # 应急压缩
                    llm_messages = self.compactor.reactive_compact(messages, self.llm)
                    try:
                        response = self.llm.chat(llm_messages, self.tools.schemas, "auto")
                    except Exception as e2:
                        return f"子任务执行失败（API调用错误）: {e2}"
                else:
                    return f"子任务执行失败（API调用错误）: {e}"

            content = response["content"]
            tool_calls = response["tool_calls"]

            # ---- LLM 没有调用工具 → 任务完成，返回文本摘要 ----
            # 子Agent的退出逻辑非常简单：LLM 不调工具就退出
            # 没有 stop_nudge（不需要强制工具调用）
            # 没有 todo 完成检查（不需要任务清单）
            if not tool_calls:
                print(f"[Subagent] 子任务完成（{iteration} 轮）")
                # 返回 LLM 的最终文本回复作为摘要
                # 如果 LLM 没有输出文本（极端情况），返回默认提示
                return content if content else "子任务已完成，但未生成摘要。"

            # ---- LLM 返回了 tool_calls → 执行工具 ----
            # 将 assistant 消息加入历史
            messages.append({
                "role": "assistant",
                "content": content or "",
                "tool_calls": tool_calls,
            })

            # 逐个执行工具调用
            for tc in tool_calls:
                name = tc["function"]["name"]
                args_str = tc["function"]["arguments"]

                try:
                    arguments = json.loads(args_str)
                except json.JSONDecodeError:
                    arguments = {}

                # ---- SQL 安全检查（基础安全，对应 s03 Gate 1）----
                # 子Agent没有 Permission 三道门管道，但保留基础的 SQL 安全检查
                # 防止子Agent执行危险SQL
                if name in ("execute_sql", "export_result", "export_merged"):
                    sql = arguments.get("sql", "")
                    from tools import _is_safe_sql  # 延迟导入，避免循环依赖
                    if not _is_safe_sql(sql):
                        result = "错误: 仅允许执行SELECT查询语句。"
                        print(f"[Subagent] SQL安全拦截: {name}")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        })
                        continue

                # 执行工具
                try:
                    result = self.tools.execute(name, arguments)
                except Exception as e:
                    result = f"工具执行错误: {e}"

                print(f"[Subagent] 工具调用: {name} → {result[:80]}...")

                # 将工具结果加入子Agent的对话历史
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        # ---- 达到最大轮次，返回超时提示 ----
        print(f"[Subagent] 子任务达到最大轮次 ({self.max_iterations})，强制终止")
        return f"子任务在 {self.max_iterations} 轮内未完成，可能需要更具体的任务描述。"
