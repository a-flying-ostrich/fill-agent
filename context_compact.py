"""
上下文压缩模块 (对应 learn-claude-code s08)

四层压缩管道 + Reactive 应急压缩：
1. snip_compact:    消息数超过阈值时，截断中间消息，保留 system + 最近 N 条
2. micro_compact:   把旧的 tool result 替换为占位符 [Earlier tool result compacted]
3. tool_result_budget: 超大 tool result 截断，只留预览
4. compact_history: 仍然超限时，调 LLM 生成历史摘要（1 次 API 调用）

Reactive: API 返回 token 超限时，激进截断 + LLM 摘要

OpenAI 格式适配：
- tool result 是 {"role": "tool", "tool_call_id": "...", "content": "..."} 独立消息
self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
- 而非 Anthropic 的 block 在 user 消息 content 列表里
- 压缩时遍历逻辑需适配此格式
"""

import os
from typing import List, Dict
import copy


class ContextCompactor:
    """
    上下文压缩器：在每次调 LLM 前执行四层压缩管道。

    设计原则（与 s08 一致）：
    - 逐层升级：先便宜的（截断），再贵的（LLM 摘要）
    - reactive 兜底：API 报 token 超限时，激进压缩 + LLM 摘要
    """

    def __init__(self, max_messages, max_tool_result_chars):
        """
        初始化压缩器。

        Args:
            max_messages:          消息数超过此值触发 snip_compact
            max_tool_result_chars: tool result 超过此字符数触发截断
        """
        self.max_messages = max_messages # config.py 里 COMPACT_MAX_MESSAGES 设置的60
        self.max_tool_result_chars = max_tool_result_chars # config.py 里 COMPACT_MAX_TOOL_RESULT_CHARS设置的30000
        self.snip_keep_recent = 60 # 普通前置截断
        self.reactive_keep_recent = 5 # 应急压缩专用
        self.micro_compact_threshold = 5000 # 微压缩阈值
        self.tool_preview_chars = 5000 # 超长 tool 结果阈值
        self.llm_summarize_compact = 500 # LLM 摘要压缩阈值

    def compact(self, messages: List[Dict], llm) -> List[Dict]:
        """
        执行四层压缩管道（在每次调 LLM 前调用）。

        顺序：snip_compact -> micro_compact -> tool_result_budget
        （compact_history 只在 reactive 时才调，避免每轮都花 API 调用）

        Args:
            messages: 当前对话历史，字典列表
            llm:      LLMClient 实例（compact_history 需要调 LLM 生成摘要）

        Returns:
            压缩后的 messages
        """
        copy_messages = copy.deepcopy(messages) # 深度拷贝，递归复制全部对象。生成一份完全独立全新副本，非常非常关键
        '''
        1. 传入compact的是 Agent 那边的原始self.messages；
        2. deepcopy 复制一份全新独立副本，后面所有 L1/L2/L3 的修改（替换 tool 内容、删掉消息、插入占位符）全部操作在这个副本上；
        3. 完全不会污染、修改 Agent 的原始 self.messages
        所有压缩操作只在副本上折腾，原始消息纹丝不动
        原始 self.messages：永久完整账本，所有真实对话、工具输入输出全部追加在这里，永远不删减、不丢失；
        压缩后的副本（copy_messages）：只是本次给 LLM 看的「精简版快照」，专门用来解决上下文窗口爆炸问题，LLM 基于这个精简快照思考、输出下一步动作；
        LLM 输出的 assistant 消息、后续工具执行结果，依然追加到【原始完整账本 self.messages】，不是追加到压缩副本。
        '''
        # L1: snip_compact — 消息数超限时截断中间消息
        if len(copy_messages) > self.max_messages:
            copy_messages = self._snip_compact(copy_messages)

        # L2: micro_compact — 旧 tool result 替换为占位符
        copy_messages = self._micro_compact(copy_messages)

        # L3: tool_result_budget — 超大 tool result 截断
        copy_messages = self._tool_result_budget(copy_messages)

        return copy_messages # 只是这个修改后的副本，交给 Agent 临时送给 LLM 调用

    def _snip_compact(self, messages: List[Dict]) -> List[Dict]:
        """
        L1 截断压缩：消息数超限时，保留 system + 最近 N 条，中间用占位符替代。

        策略（与 s08 一致）：
        - 保留 messages[0]（system prompt，不能丢）
        - 保留最近 keep_recent 条（当前正在处理的上下文）
        - 中间的全部用一条占位符消息替代
        """
        keep_recent = self.snip_keep_recent  # 保留最近 40 条消息

        if len(messages) <= keep_recent + 1:
            return messages  # 不需要截断

        system_msg = messages[0]  # system prompt 必须保留，单个消息字典

        # 计算初始切点
        cut = len(messages) - keep_recent

        # 调整切点：如果切点落在 tool 消息上，往前回退到对应的 assistant 消息
        # 防止截断后 recent 里出现孤立的 tool 消息（没有对应 assistant 的 tool_calls）
        while cut > 1 and messages[cut].get("role") == "tool":
            cut -= 1

        recent = messages[cut:]  # 从切点到末尾，字典列表
        skipped = cut - 1  # 被跳过的消息数（不含 system）

        placeholder = {
            "role": "assistant",
            "content": f"[Earlier {skipped} messages snipped to save context space]",
        } # 单个消息字典

        return [system_msg, placeholder] + recent # 把两个字典包装成一个新列表，然后列表与列表相加

    def _micro_compact(self, messages: List[Dict]) -> List[Dict]:
        """
        L2 微压缩：把旧的 tool result 替换为占位符。

        OpenAI 格式适配：
        - tool result 是 {"role": "tool", "tool_call_id": "...", "content": "..."}
        - 保留最近五条 tool result 完整（当前正在用的）
        - 其余超过 5000 字符的 tool result 替换为占位符
        """
        # 找到所有 tool 消息的索引
        tool_indices = [
            i for i, m in enumerate(messages) if m.get("role") == "tool"
        ] # 索引列表

        if len(tool_indices) <= 5:
            return messages  # 小于 5 条 tool result，不需要压缩

        # 保留最近五条 tool result 完整，其余压缩
        for i in tool_indices[:-5]:
            content = messages[i].get("content", "")
            if len(content) > self.micro_compact_threshold:
                messages[i] = {
                    "role": "tool",
                    "tool_call_id": messages[i].get("tool_call_id", ""),
                    "content": "[Earlier tool result compacted to save context space]",
                }

        return messages

    def _tool_result_budget(self, messages: List[Dict]) -> List[Dict]:
        """
        L3 大结果预算：超大的 tool result 截断，只留预览。专门给【最近 5 条 tool result】服务
        经过 micro_compact 之后，旧 tool 消息的 content 一定很短，不会再触发 L3 截断。

        策略（与 s08 一致）：
        当 tool result 超过 max_tool_result_chars 字符时：
        - 保留前 5000 字符作为预览
        - 加上截断提示
        - 原始完整结果丢弃（如果有需要，LLM 可以重新查询）
        """
        for i, m in enumerate(messages):
            if m.get("role") == "tool":
                content = m.get("content", "")
                if len(content) > self.max_tool_result_chars:
                    preview = content[:self.tool_preview_chars]
                    messages[i] = {
                        "role": "tool",
                        "tool_call_id": m.get("tool_call_id", ""),
                        "content": (
                            preview
                            + f"\n...[Full result ({len(content)} chars) "
                            f"truncated to save context space]"
                        ),
                    }

        return messages

    def reactive_compact(self, messages: List[Dict], llm) -> List[Dict]:
        """
        只有 Reactive 应急压缩才调用这个函数！正常compact()不会调用它，调用 LLM 生成摘要耗 token。非万不得已不要每一轮都生成摘要。
        应急压缩：API 返回 token 超限时调用。

        每一个大模型都有上下文窗口（Context Window），代表模型一次最多能处理多少个token。
        例：Qwen2.5‑7B‑Instruct 常见是 32K 上下文，代表：输入 + 输出合计最多约 32768 个 token。
        你送给模型的messages整套对话历史（system 提示、用户提问、assistant 回答、tool 工具返回）全部会被分词器转成 token。
        当输入的总 token 数量 > 模型最大上下文窗口，后端（vLLM / OpenAI API）就会抛出 token 超限异常
        注意：不是字符！是token，中文大概 1token≈1.5‑2 个汉字。
        一万汉字 ≈ 5000~7000 token。

        策略（与 s08 一致）：
        - 激进截断：只保留 system + 最近 5 条
        - 用 LLM 生成旧对话的历史摘要（1 次 API 调用）
        - 将摘要作为一条 user 消息注入

        Args:
            messages: 当前对话历史
            llm:      LLMClient 实例（用于生成摘要）

        Returns:
            压缩后的 messages
        """
        keep_recent = self.reactive_keep_recent  # 应急模式下只保留最近 5 条

        if len(messages) <= keep_recent + 1:
            return messages  # 已经很短了，不需要压缩

        system_msg = messages[0] # system prompt 必须保留，单个消息字典
        old_messages = messages[1:-keep_recent] # 要扔掉的一大段旧消息
        recent = messages[-keep_recent:] # 保留最近5条完整消息

        # 用 LLM 生成历史摘要
        summary = self._llm_summarize(old_messages, llm)
        summary_msg = {
            "role": "user",
            "content": f"[Conversation history summary]: {summary}",
        }

        return [system_msg, summary_msg] + recent

    def _llm_summarize(self, old_messages: List[Dict], llm) -> str:
        """
        把一大段被裁剪掉的旧对话消息，整理成文本，调用大模型生成一段文字摘要，用来替代一大堆原始消息，减少 token 消耗
        调用 LLM 生成对话历史摘要。

        把旧消息拼接成文本，让 LLM 生成一份简要摘要，
        保留关键信息（执行了什么操作、结果如何、用户的核心需求）。

        Args:
            old_messages: 要摘要的旧消息列表
            llm:      LLMClient 实例

        Returns:
            摘要文本字符串
        """
        text_parts = []
        for m in old_messages:
            role = m.get("role", "unknown") # 取出每条消息的role角色、content内容
            content = m.get("content", "")
            if isinstance(content, str) and content:
                # 每条消息只取前 500 字符，避免摘要输入过长
                text_parts.append(f"[{role}]: {content[:self.llm_summarize_compact]}")

        if not text_parts:
            return "No history to summarize."

        text = "\n".join(text_parts)

        try: # 调用 LLMClient.chat 生成摘要
            response = llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是一个对话摘要助手。请用中文简要总结以下对话历史，"
                            "保留关键信息：执行了什么操作、结果如何、用户的核心需求。"
                            "摘要控制在 500 字以内。"
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                None,  # 第二个参数传None：关闭 Function‑Calling 工具，这里只做纯文本总结，不需要调用任何工具
            )
            return response.get("content", "摘要生成失败")
        except Exception as e:
            print(f"[上下文压缩] 摘要生成失败: {e}")
            return "摘要生成失败，已跳过早期对话。"
