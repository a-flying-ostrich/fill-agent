"""
跨会话记忆模块 (对应 learn-claude-code s09)

三个子系统（与 s09 一致）：
1. 筛选（Filtering）：哪些对话内容值得记忆
   - 用户偏好（字段映射习惯、常用导出路径）
   - 项目事实（表结构、字段含义）
   - 操作模式（典型查询模式、常见问题处理方式）
2. 提取（Extraction）：结构化为 key-value 存入记忆文件
   - 用 LLM 分析对话，识别值得记忆的内容
   - 结构化为 category/key/value/description 四元组
   - 存入 .memory/memories/ 目录的 Markdown 文件
3. 整理（Consolidation）：记忆文件过多时合并去重
   - 当记忆数量超过阈值时触发
   - 用 LLM 合并相似记忆、删除过时记忆

存储格式：
    .memory/
    ├── MEMORY.md              # 索引文件（每轮加载到 SYSTEM，便宜，约 200 tokens）
    └── memories/ # 文件夹
        ├── memory_001.md      # 具体记忆文件（按需读取，不提前注入）
        ├── memory_002.md
        └── ...

每个记忆文件格式（Markdown + YAML frontmatter）：
    ---
    category: preference       # preference(用户偏好) / fact(项目事实) / pattern(操作模式)
    key: 字段映射偏好            # 记忆键名
    value: 年级 → 所在年级      # 记忆值
    created_at: 2026-09-09      # 创建时间
    ---
    用户在查询时习惯用"年级"，但表中字段名是"所在年级"...

适合填表场景：
- 记住用户的字段映射偏好（如"年级"对应"所在年级"）
- 记住常用导出路径（如 C:/output/）
- 记住表结构信息（如学生信息表有哪些字段）
- Agent 跨会话记住用户偏好，实现学习能力

大概流程：对话 → LLM 提取记忆输出 JSON（category/key/value/description） → _save_memory 代码手动包上前后 --- YAML 标记，写入独立 memory_xxx.md → _load_all_memories 读取所有 md 的 frontmatter 元数据 → _update_index 生成不带 --- 的 MEMORY.md 索引

对外暴露的两个核心方法（agent.py中会用到）：
1. extract_memories()：对话结束后提取记忆，新增记忆 md 本体文件，调用_update_index()刷新 MEMORY.md 索引，自动调用 _consolidate 检查是否超限
2. get_memory_index()：读取已经生成好的 MEMORY.md，返回索引文本注入 System Prompt（只读，不修改任何文件）
"""

import os
import json
from datetime import datetime
from typing import List, Dict, Optional

class MemoryManager:
    """
    记忆管理器：负责记忆的提取、存储、索引和整理。

    使用方式：
        manager = MemoryManager(".memory") # 代码运行的当前工作目录 → 里面新建 .memory 文件夹
        index = manager.get_memory_index()     # 获取索引文本，注入 SYSTEM
        manager.extract_memories(messages, llm)  # 对话结束后提取记忆（内部自动 _consolidate）
    """

    def __init__(self, memory_dir: str, max_memories: int = 20):
        """
        初始化记忆管理器，创建目录结构。

        Args:
            memory_dir:   记忆根目录路径（如 ".memory"）
            max_memories:  记忆数量上限，超过此值触发整理（合并去重）
        """
        self.memory_dir = memory_dir
        self.memories_dir = os.path.join(memory_dir, "memories") # os.path.join 拼接路径，只做拼接，memories是文件夹
        self.index_file = os.path.join(memory_dir, "MEMORY.md") # MEMORY.md是文件，文件必须通过 open(xxx, "w") 写入内容才会在磁盘上生成
        self.max_memories = max_memories # 具体记忆文件20个

        # 确保目录存在
        self._ensure_dirs() # 创建路径

    def _ensure_dirs(self):
        """创建记忆目录结构（.memory/ 和 .memory/memories/）。"""
        os.makedirs(self.memories_dir, exist_ok=True) # 一次性创建整条路径

    # ========================================================
    # 子系统 1 + 2：筛选 + 提取（合在一起实现）
    # ========================================================

    def extract_memories(self, messages: List[Dict], llm) -> int:
        """
        筛选 + 提取：从对话历史中提取值得记忆的内容。

        流程（与 s09 一致）：
        1. 取最近的对话消息（避免处理过长的历史）
        2. 调 LLM 分析对话，识别值得记忆的内容
        3. LLM 返回结构化的记忆列表（JSON 格式）
        4. 逐条保存为记忆文件
        5. 更新 MEMORY.md 索引
        6. 自动调用 _consolidate 检查记忆数量是否超限

        Args:
            messages: 对话历史（self.messages）
            llm:      LLMClient 实例（用于记忆提取的 API 调用）

        Returns:
            本次提取的记忆数量
        """
        # 取最近的 20 条消息进行分析（避免输入过长）
        recent = messages[-20:] if len(messages) > 20 else messages

        # 拼接对话文本给到text
        text_parts = []
        for m in recent:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if isinstance(content, str) and content:
                # 每条消息取前 500 字符，避免摘要输入过长
                text_parts.append(f"[{role}]: {content[:500]}")

        if not text_parts:
            return 0

        text = "\n".join(text_parts)

        # 调 LLM 提取记忆
        new_memories = self._llm_extract(text, llm) # 返回字典列表

        if not new_memories:
            return 0

        # 保存记忆文件
        for mem in new_memories:
            self._save_memory(
                category=mem.get("category", "fact"),
                key=mem.get("key", ""),
                value=mem.get("value", ""),
                description=mem.get("description", ""),
            )

        # 更新索引
        self._update_index() # 更新一下MEMORY.md文件

        # 自动检查记忆数量，超限则合并去重
        self._consolidate(llm)

        print(f"[Memory] 提取了 {len(new_memories)} 条新记忆")
        return len(new_memories)

    def _llm_extract(self, text: str, llm) -> List[Dict]:
        """
        调用 LLM 从对话文本中提取值得记忆的内容。

        LLM 返回 JSON 数组，每项包含:
        - category（种类，范畴）: preference(用户偏好) / fact(项目事实) / pattern(操作模式)
        - key: 记忆键名（简短，如"字段映射偏好"）
        - value: 记忆值（简短，如"年级 → 所在年级"）
        - description: 详细描述（1-2 句话）

        Args:
            text: 对话文本
            llm:  LLMClient 实例

        Returns:
            记忆字典列表，提取失败返回空列表
        """
        extract_prompt = (
            "请分析以下对话历史，提取值得跨会话记忆的内容。\n"
            "只提取有长期价值的信息，分为3类，category只能使用下面三个英文单词，禁止中文：\n"
            "- preference：用户偏好（字段映射习惯、常用导出路径、输出格式偏好）\n"
            "- fact：项目事实（表结构、字段含义、数据规律）\n"
            "- pattern：操作模式（典型查询模式、常见问题处理方式）\n\n"
            "不要提取一次性的操作步骤、临时问答、转瞬即逝信息。\n"
            "如果没有值得记忆的内容，直接返回空数组 []。\n\n"
            "请返回 JSON 数组格式，每条记忆必须包含 category/key/value/description 四个字段。\n"
            "示例：\n"
            '[{"category": "preference", "key": "字段映射偏好", "value": "年级 → 所在年级", "description": "用户习惯用年级，但表字段名称是所在年级"},\n'
            '{"category": "fact", "key": "学生表字段", "value": "学生信息表包含：姓名、学号、所在年级", "description": "项目事实，记录学生表的字段清单"},\n'
            '{"category": "pattern", "key": "查询习惯", "value": "用户习惯按年级分组查询数据", "description": "用户常用的查询操作模式"}]\n\n'
            f"对话历史：\n{text}"
        )

        try:
            response = llm.chat(
                [
                    {
                        "role": "system",
                        "content": "你是一个记忆提取助手。请分析对话，提取值得长期记忆的信息，以 JSON 数组格式返回。",
                    },
                    {"role": "user", "content": extract_prompt},
                ],
                None,  # 关闭 Function Calling，纯文本提取
            )
            content = response.get("content", "")

             # 解析 JSON 响应
            # LLM 可能返回带前缀文本、markdown 代码块标记的响应
            # 策略：找到第一个 [ 和最后一个 ]，提取中间的 JSON 数组
            content = content.strip()

            # 去掉 markdown 代码块标记（```json ... ```）
            if "```" in content:
                # 找到第一个 ``` 之后的内容
                parts = content.split("```")
                if len(parts) >= 3:
                    # 取第二个代码块（```json 和 ``` 之间）
                    content = parts[1]
                    # 去掉开头的语言标记（如 json）
                    if content.startswith("json"):
                        content = content[4:]
                    content = content.strip()

            # 提取 JSON 数组：找第一个 [ 和最后一个 ]
            start = content.find("[")
            end = content.rfind("]")
            if start != -1 and end != -1 and end > start:
                content = content[start:end + 1]

            memories = json.loads(content) # 将json数组转化成字典列表
            
            if isinstance(memories, list):
                return memories
            return []
        except json.JSONDecodeError:
            print("[Memory] LLM 返回的 JSON 解析失败，跳过本次提取")
            return []
        except Exception as e:
            print(f"[Memory] 记忆提取失败: {e}")
            return []

    def _save_memory(self, category: str, key: str, value: str, description: str):
        """
        保存一条记忆为 Markdown 文件。

        具体记忆文件memory_00x.md文件格式：
            ---
            category: preference
            key: 字段映射偏好
            value: 年级 → 所在年级
            created_at: 2026-09-09 23:40:00
            ---
            详细描述...

        Args:
            category:    记忆类别（preference/fact/pattern）
            key:         记忆键名
            value:       记忆值
            description: 详细描述
        """
        # 生成唯一文件名：时间戳 + key 的哈希
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_key = "".join(c if c.isalnum() or c in "._-" else "_" for c in key[:20])
        filename = f"memory_{timestamp}_{safe_key}.md"
        file_path = os.path.join(self.memories_dir, filename)

        # 构建 Markdown 内容
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = (
            f"---\n"
            f"category: {category}\n"
            f"key: {key}\n"
            f"value: {value}\n"
            f"created_at: {now}\n"
            f"---\n\n"
            f"{description}\n"
        ) # 拼接后的单个字符串

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
        except IOError as e:
            print(f"[Memory] 保存记忆文件失败: {e}")

    # ========================================================
    # 索引管理，处理生成MEMORY.md文件
    # ========================================================

    def _load_all_memories(self) -> List[Dict]:
        """
        遍历 memories/ 文件夹，批量读取所有 md 的 frontmatter，只用于生成 / 更新索引，不是给 LLM 直接看的

        读取所有记忆文件的元数据（category, key, value, created_at）。

        不读取正文描述，只读 frontmatter，保持轻量。

        Returns:
            记忆元数据字典列表，包括：每一个列表都包括路径, category, key, value, created_at这五个键
        """
        memories = []
        if not os.path.exists(self.memories_dir):
            return memories

        for filename in sorted(os.listdir(self.memories_dir)):
            if not filename.endswith(".md"):
                continue
            file_path = os.path.join(self.memories_dir, filename)
            metadata = self._parse_frontmatter(file_path)
            if metadata:
                metadata["file_path"] = file_path
                memories.append(metadata)

        return memories

    def _parse_frontmatter(self, file_path: str) -> Optional[Dict]:
        """
        解析记忆文件的 YAML frontmatter（与 SkillLoader 类似的简单解析）。

        Args:
            file_path: 记忆文件路径

        Returns:
            包含 category/key/value/created_at 的字典
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except IOError:
            return None

        if not content.strip().startswith("---"):
            return None

        lines = content.split("\n")
        if lines[0].strip() != "---":
            return None

        end_index = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_index = i
                break

        if end_index is None:
            return None

        metadata = {}
        for line in lines[1:end_index]:
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip()

        return metadata

    def _update_index(self):
        """
        重新生成 MEMORY.md 索引文件。

        索引格式（没有任何---，纯 Markdown 目录文本）：
            # 记忆索引

            ## 用户偏好
            - [字段映射偏好] 年级 → 所在年级 (2026-09-09)
            - [导出路径] C:/output/ (2026-09-09)

            ## 项目事实
            - [表结构] 学生信息表有字段: 姓名、学号、年级 (2026-09-09)

        索引文件每轮加载到 SYSTEM，约 200 tokens，非常便宜。
        """
        memories = self._load_all_memories()

        # 按类别分组
        categories = {"preference": "用户偏好", "fact": "项目事实", "pattern": "操作模式"}
        grouped: Dict[str, List[Dict]] = {cat: [] for cat in categories.values()}

        for mem in memories:
            # 先读取记忆里的分类字段，缺失默认 fact
            mem_category_en = mem.get("category", "fact")
            # 将英文分类映射为中文分组名称，找不到则兜底：项目事实
            cat = categories.get(mem_category_en, "项目事实")
            grouped[cat].append(mem)

        # 构建索引文本
        lines = ["# 记忆索引\n"]
        for cat_name, mems in grouped.items():
            if not mems:
                continue
            lines.append(f"## {cat_name}") # 二级标题
            for mem in mems:
                # 取出key、value、截取日期（只保留年月日，砍掉时分秒）
                key = mem.get("key", "")
                value = mem.get("value", "")
                created = mem.get("created_at", "")[:10]  # 只取日期
                lines.append(f"- [{key}] {value} ({created})")
            lines.append("")

        try:
            with open(self.index_file, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except IOError as e:
            print(f"[Memory] 更新索引失败: {e}")

    def get_memory_index(self) -> str:
        """
        获取记忆索引文本 MEMORY.md，注入 SYSTEM_PROMPT。

        每轮调 LLM 前将索引注入 system 消息，让 Agent 知道之前的记忆。
        索引很轻量（约 200 tokens），不会显著增加 token 消耗。

        如果没有记忆文件，返回空字符串。

        Returns:
            记忆索引文本
        """
        if not os.path.exists(self.index_file):
            return "" # 如果 MEMORY.md 文件根本不存在，返回空字符串

        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                content = f.read()
            if content.strip():
                return f"\n## 跨会话记忆\n{content}"
        except IOError:
            pass

        return ""

    # ========================================================
    # 子系统 3：整理（Consolidation）— 内部方法，由 extract_memories 自动调用
    # ========================================================

    def _consolidate(self, llm) -> int:
        """
        整理：当记忆数量超过阈值时，用 LLM 合并去重。

        流程（与 s09 一致）：
        1. 读取所有记忆文件
        2. 发送给 LLM，让它合并相似记忆、删除过时记忆
        3. 用合并后的记忆替换旧文件
        4. 更新索引

        由 extract_memories 自动调用，无需外部手动调用。
        未超限时直接返回，几乎零开销。

        Args:
            llm: LLMClient 实例

        Returns:
            整理后的记忆数量
        """
        memories = self._load_all_memories()

        if len(memories) < self.max_memories:
            return len(memories)  # 数量未超阈值，不需要整理

        print(f"[Memory] 记忆数量 {len(memories)} 超过阈值 {self.max_memories}，开始整理...")

        # 构建记忆摘要文本
        text_parts = []
        for i, mem in enumerate(memories):
            text_parts.append(
                f"[{i}] category={mem.get('category', '')}, "
                f"key={mem.get('key', '')}, "
                f"value={mem.get('value', '')}"
            )

        text = "\n".join(text_parts)

        # 调 LLM 合并去重
        consolidate_prompt = (
            "请整理以下记忆列表，合并相似记忆、删除过时记忆。\n"
            "返回合并后的记忆列表，JSON 数组格式，每项包含 category/key/value/description。\n"
            "合并规则：\n"
            "1. 相同 key 的记忆合并为一条，value 取最新值\n"
            "2. 语义重复的记忆合并为一条\n"
            "3. 明显过时的记忆可以删除\n\n"
            f"记忆列表：\n{text}"
        )

        try:
            response = llm.chat(
                [
                    {
                        "role": "system",
                        "content": "你是一个记忆整理助手。请合并去重记忆列表，返回 JSON 数组。",
                    },
                    {"role": "user", "content": consolidate_prompt},
                ],
                None,
            )
            content = response.get("content", "")

            # 清理 markdown 代码块标记
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()

            consolidated = json.loads(content)
            if not isinstance(consolidated, list):
                return len(memories)

            # 删除旧记忆文件
            for mem in memories:
                try:
                    os.remove(mem["file_path"])
                except (IOError, KeyError):
                    pass

            # 保存整理后的记忆，保存每一个单条md文件
            for mem in consolidated:
                self._save_memory(
                    category=mem.get("category", "fact"),
                    key=mem.get("key", ""),
                    value=mem.get("value", ""),
                    description=mem.get("description", ""),
                )

            # 更新索引
            self._update_index()

            result_count = len(consolidated)
            print(f"[Memory] 整理完成，从 {len(memories)} 条合并为 {result_count} 条")
            return result_count

        except json.JSONDecodeError:
            print("[Memory] 整理结果 JSON 解析失败，跳过本次整理")
            return len(memories)
        except Exception as e:
            print(f"[Memory] 记忆整理失败: {e}")
            return len(memories)