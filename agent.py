"""
Agent Loop 主循环(核心)

核心架构（与 learn-claude-code 的 Agent Loop 模式一致）：
- LLM 作为决策中枢，自主决定调用哪些工具、以什么顺序调用
- Harness（本文件 + tools.py）提供工具执行环境
- 循环流程：
    LLM 思考 -> 返回 tool_calls -> 执行工具 -> 结果回传 LLM -> 继续思考
    -> ... -> LLM 返回纯文本（无 tool_calls）-> 任务完成

关键设计：
- 对话历史跨多轮用户输入保持，Agent 能记住之前的操作
- 工具执行错误不抛异常，而是返回错误字符串让 LLM 自行处理
- 最大迭代次数限制，防止无限循环

升级内容（Phase 1）：
1. Hooks 钩子体系 (s04): HOOKS 注册表 + 4 类钩子（PreToolUse/PostToolUse/Stop/UserPromptSubmit）
2. Permission 权限系统 (s03): 三道门管道，作为 PreToolUse 钩子实现
3. TodoWrite 任务规划 (s05): nag 提醒计数器，连续 N 轮未更新清单就注入提醒
4. Context Compact 上下文压缩 (s08): 四层压缩管道 + reactive 应急压缩

升级内容（Phase 2）：
5. Memory 跨会话记忆 (s09): 对话结束后自动提取记忆，跨会话记住用户偏好和项目事实
6. Skill Loading 按需知识加载 (s07): SYSTEM_PROMPT 动态组装，技能目录注入 SYSTEM，全文按需加载
7. Subagent 子Agent (s06): task 工具派生子Agent，上下文隔离，保护主Agent上下文不被子任务细节污染
"""
import time
import json
import os
import re
from types import SimpleNamespace  # 适配器，让 hook 代码用 block.name / block.input 访问，不用改
from typing import List, Dict, Optional, Callable
from llm_client import LLMClient
from tools import ToolManager  # _is_safe_sql 已搬到 agent.py，不再从 tools 导入
from context_compact import ContextCompactor
from skill_loader import SkillLoader  # Phase 2 新增：技能加载 (s07)
from memory import MemoryManager     # Phase 2 新增：跨会话记忆 (s09)
# ===== 改动：从 config 导入配置常量，不再在代码里硬编码魔术数字 =====
from config import (
    COMPACT_MAX_MESSAGES, COMPACT_MAX_TOOL_RESULT_CHARS,
    TODO_NAG_THRESHOLD, MAX_STOP_NUDGES,
    SKILLS_DIR, MEMORY_DIR, MEMORY_MAX_COUNT,  # Phase 2 新增
)
'''
# 消息数超过此值触发 snip_compact（截断中间消息）
COMPACT_MAX_MESSAGES = 60
# tool result 超过此字符数触发截断（只留预览）
COMPACT_MAX_TOOL_RESULT_CHARS = 30000
# nag 提醒阈值：连续 N 轮没更新 todo_write 就注入提醒 (对应 s05)
TODO_NAG_THRESHOLD = 3
# Stop nudge 最大提醒次数：LLM 不输出 tool_calls 时最多提醒 N 次后放弃
MAX_STOP_NUDGES = 3
# Phase 2 新增：
SKILLS_DIR = "skills"
MEMORY_DIR = ".memory"
MEMORY_MAX_COUNT = 20
'''

# ========================================================
# SQL 安全检查（从 tools.py 搬迁至此）
# 仅在 permission_hook 中调用，tools.py 内部不使用
# ========================================================

# 危险关键字列表：这些关键字如果出现在 SQL 中，说明可能不是纯 SELECT 查询
_DANGEROUS_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER",
    "CREATE", "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX",
]

def _is_safe_sql(sql: str) -> bool:
    """
    检查 SQL 语句是否安全（仅允许 SELECT 或 WITH...SELECT）。

    检查规则：
    1. 必须以 SELECT 或 WITH 开头
    2. 不能包含危险关键字（INSERT/UPDATE/DELETE/DROP 等）

    注意：这是一个基础检查，无法处理字符串字面量中包含关键字的情况
    （如 WHERE name = 'DELETE'）。对于学习项目足够使用，
    生产环境应使用 SQLite 的 authorizer 回调做更严格的控制。

    Args:
        sql: 待检查的 SQL 语句

    Returns:
        True 表示安全，False 表示不安全
    """
    sql_upper = sql.strip().upper() # 去掉字符串首尾空白字符, 并转换为大写upper，方便后续判断
    if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
        return False
    for kw in _DANGEROUS_KEYWORDS:
        if re.search(r"\b" + kw + r"\b", sql_upper): # 在文本 sql_upper 里搜索正则模式 r"\b" + kw + r"\b"，只要任意位置匹配成功就返回匹配对象；匹配不到返回 None
            return False
    return True

# ========================================================
# Hooks 钩子体系 (对应 s04)
# "挂在循环上，不写进循环里"
# 新增横切逻辑（日志、权限、格式化）只需 register_hook，不用改 run()
# ========================================================

# HOOKS 注册表：事件名 -> 回调函数列表
# 4 类钩子事件：
# - UserPromptSubmit: 用户输入到达 LLM 前触发（可注入上下文）
# - PreToolUse:       工具执行前触发（权限检查、日志记录）
# - PostToolUse:      工具执行后触发（结果格式化、截断警告）
# - Stop:             LLM 停止调用工具时触发（完成检查、提醒）
HOOKS: Dict[str, List[Callable]] = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}

def register_hook(event: str, callback: Callable): # `Callable` 是 Python **typing 类型注解**里的一个类型，直译：**可调用对象**
    """
    注册钩子：把回调函数加入指定事件的列表。

    使用方式：
        register_hook("PreToolUse", my_permission_check)

    之后每次工具执行前，my_permission_check 都会被自动调用，
    不需要修改 run() 方法里的任何代码。

    Args:
        event:    钩子事件名（"UserPromptSubmit" / "PreToolUse" / "PostToolUse" / "Stop"）
        callback: 回调函数
    """
    if event in HOOKS:
        HOOKS[event].append(callback)

def trigger_hooks(event: str, *args) -> Optional[str]: # 触发核心函数（最重要），这里的*args就是收集
    """
    触发钩子：依次调用该事件的所有回调函数。

    返回值规则（与 s04/s05 一致）：
    - 返回 None：放行，继续执行原逻辑
    - 返回字符串：拦截，用该字符串替代原始行为

    多个钩子的优先级：先注册的先执行，任一钩子返回非 None 就拦截，后续钩子不再执行。

    Args:
        event: 钩子事件名
        *args: 传给钩子回调的参数，把当前事件需要的数据传给回调函数callback

    Returns:
        None 表示放行，字符串表示拦截
    """
    for cb in HOOKS.get(event, []):
        result = cb(*args) # 这里的*args就是解包
        if result is not None:
            return result# 阻断的问题
    return None
'''
*args 里单个 * 作用
收集「多余的位置参数」，打包成元组 tuple
调用：
trigger_hooks("click", 100, 200, "test")
event = "click"
剩下 100, 200, "test" 全部被 *args 接收
args = (100, 200, "test") 👉 元组，不可变
✅ 定义函数时：* = 收集位置参数
2. 你知道的 **kwargs（两个星号）
收集「多余的命名参数」，打包成字典 dict
def func(*args, **kwargs):
    pass
func(1,2,3, name="zs", age=24)
# args = (1,2,3)
# kwargs = {"name":"zs","age":24}

重点区分【定义函数】VS【调用函数】
很多人混淆！两种场景 * 含义完全相反：
场景 A：写在函数 def 行（收集）
def f(*args):
* = 收集多个位置参数 → 元组
def f(**kwargs):
** = 收集多余的命名参数 → 字典
场景 B：调用函数时（解包，就是你认识的功能）
nums = (1,2,3)
f(*nums)   # *把元组拆开，逐个传入（解包）
info = {"a":1, "b":2}
f(**info)  # **把字典拆开，变成 key=value 传入

为什么不直接传入元组和字典，非要收集和解包呢。收集是为了变成元组和字典，解包是为了拆掉元组和字典
API 调用方写代码的体验天差地别
方案 1：不用 *，强制手动传元组（笨重写法）
# 设计1：参数直接接收元组
def trigger_hooks(event: str, args: tuple):
    ...
# 调用的时候，必须手动套括号
trigger_hooks("mouse_move", (120, 350))
trigger_hooks("click", ())  # 无额外参数，还要传空元组！很烦
方案 2：使用 *args（优雅写法）
def trigger_hooks(event: str, *args):
    # 函数内部 args 天然就是元组，和上面一模一样
    ...
# 调用，随便写参数，不用括号包裹
trigger_hooks("mouse_move", 120, 350)
trigger_hooks("click") # 不需要额外写空元组
'''

# ========================================================
# 5 个 standalone 钩子函数（全部在类外面，对应 s04/s05 设计）
# 用 SimpleNamespace block 包装工具调用信息，hook 代码用 block.name / block.input 访问
# ===== 改动：stop_nudge_hook 从 standalone 改为 __init__ 里的闭包，因为它需要访问实例属性 =====
# ========================================================

# ---- 1. UserPromptSubmit 钩子 ----
def context_inject_hook(query: str) -> Optional[str]:
    """
    UserPromptSubmit 钩子：用户输入到达 LLM 前触发。

    可以在这里注入额外上下文（如当前数据库有哪些表）。
    目前只做日志记录，不注入额外内容。

    返回 None：不注入额外内容
    返回字符串：注入到 messages 里作为额外上下文
    """
    preview = query[:80]
    print(f"[HOOK] UserPromptSubmit | Query: {preview}...")
    return None

# ---- 2. PreToolUse 钩子：日志记录（执行前打印，与 s05 log_hook 一致）----
def log_hook(block) -> Optional[str]:
    """
    PreToolUse 钩子：工具执行前打印日志。

    在工具执行前，打印一条日志，记录模型准备调用什么工具。
    永远返回 None（不拦截执行）。

    用 block.name 访问工具名（SimpleNamespace 属性访问，比字典 ["name"] 更顺滑）
    """
    print(f"[HOOK] PreToolUse | {block.name}")
    return None

# ---- 3. PreToolUse 钩子：权限检查（对应 s03）----
def permission_hook(block) -> Optional[str]:
    """
    PreToolUse 钩子：工具执行前的权限检查。

    三道门管道（与 s03 一致）：
    - Gate 1 硬拒：SQL 出现 DROP/DELETE 等危险关键字 -> 直接拒绝
    - Gate 1b NULL检测：export_merged 的 SQL 里用 NULL AS 时 -> 问用户
    - Gate 2 路径确认：导出前确认路径，文件已存在 -> 问是否覆盖
    - Gate 3 导入确认：导入 Excel 前显示文件路径并确认

    返回 None：放行，工具正常执行
    返回字符串：拦截，该字符串作为工具执行结果返回给 LLM
    """
    # Gate 1: SQL 安全检查（硬拒）
    if block.name in ("execute_sql", "export_result"):
        sql = block.input.get("sql", "")
        if not _is_safe_sql(sql):
            return "错误: 仅允许执行SELECT查询语句，不能执行INSERT/UPDATE/DELETE/DROP等操作。"

    if block.name == "export_merged":
        tables_queries = block.input.get("tables_queries", [])
        for item in tables_queries:
            sql = item.get("sql", "")
            if not _is_safe_sql(sql):
                table_name = item.get("table_name", "未知表")
                return f"错误: 表 {table_name} 的SQL不安全，仅允许SELECT查询语句。"
            # Gate 1b: NULL AS 检测，字段缺失时必须问用户，不能擅自用NULL
            if "NULL AS" in sql.upper():
                table_name = item.get("table_name", "未知表")
                print(f"\n[权限确认] 表 {table_name} 的SQL中使用了 NULL AS 占位字段")
                print(f"SQL: {sql}")
                choice = input("是否允许用NULL替代缺失字段？[y/N] ").strip()
                if choice.lower() not in ("y", "yes"):
                    return f"用户不允许对表 {table_name} 使用NULL占位，请调用ask_user询问用户如何处理缺失字段。"

    # Gate 2: 导出路径确认 + 覆盖确认
    if block.name in ("export_result", "export_merged"):
        file_path = block.input.get("file_path", "")
        print(f"\n[权限确认] 导出路径: {file_path}")
        choice = input("确认导出到此路径？[Y/n] ").strip()
        if choice.lower() in ("n", "no"):
            return "用户取消了导出操作。"
        # 文件已存在时再问是否覆盖
        if file_path and os.path.exists(file_path):
            print(f"[权限确认] 文件已存在: {file_path}")
            choice2 = input("文件已存在，是否覆盖？[y/N] ").strip()
            if choice2.lower() not in ("y", "yes"):
                return "用户取消了导出操作，文件未被覆盖。"

    # Gate 3: 导入前确认（用户确认）
    if block.name == "import_excel":
        file_path = block.input.get("file_path", "")
        print(f"\n[权限确认] 即将导入文件: {file_path}")
        choice = input("确认导入？[Y/n] ").strip()
        if choice.lower() in ("n", "no"):
            return "用户取消了导入操作。"

    return None  # 放行

# ---- 4. PostToolUse 钩子：超大输出告警（与 s05 large_output_hook 一致）----
def large_output_hook(block, output: str) -> Optional[str]:
    """
    PostToolUse 钩子：工具执行后检测超大输出。

    如果工具返回的结果超过 10000 字符，打印警告。
    不拦截执行（始终返回 None）。

    用 block.name 访问工具名（SimpleNamespace）
    """
    if len(str(output)) > 10000:
        print(f"[HOOK] Large output from {block.name}: {len(str(output))} chars")
    return None

# ===== 改动：standalone 的 stop_nudge_hook 删除，移到 __init__ 里做闭包 =====
# 原因：stop_nudge_hook 需要读写 self._stop_nudge_count 和 self._max_stop_nudges，
# standalone 函数无法访问实例属性，之前用 global 变量解决，现在改为闭包捕获 self。

class FillAgent:
    """填表助手 Agent：基于 Agent Loop 架构"""

    def __init__(self,api_key: str, base_url: str, model: str, db_path: str, max_iterations: int, system_prompt: str,):
        """
        初始化 Agent。

        Args:
            api_key:        DashScope API Key
            base_url:       DashScope OpenAI 兼容接口地址
            model:          模型名称
            db_path:        SQLite 数据库文件路径
            max_iterations: Agent Loop 最大迭代次数
            system_prompt:  系统提示词（定义 Agent 行为）
        """
        self.llm = LLMClient(api_key, base_url, model)

        # ===== Phase 2 新增：SkillLoader 技能加载器 (对应 s07) =====
        # 启动时扫描 skills/ 目录，构建技能注册表
        # 技能目录（名称+描述）注入 SYSTEM，全文按需加载
        self.skill_loader = SkillLoader(SKILLS_DIR)

        # ===== Phase 2 新增：MemoryManager 记忆管理器 (对应 s09) =====
        # 管理跨会话记忆，对话结束后自动提取记忆
        # 记忆索引注入 SYSTEM，具体记忆文件按需读取
        self.memory = MemoryManager(MEMORY_DIR, MEMORY_MAX_COUNT)

        # ===== Phase 2 改动：ToolManager 传入 llm_client 和 skill_loader =====
        # task 工具需要 llm_client 创建子Agent
        # load_skill 工具需要 skill_loader 加载技能全文
        self.tools = ToolManager(
            db_path,
            llm_client=self.llm,         # Phase 2 新增：task 工具需要
            skill_loader=self.skill_loader, # Phase 2 新增：load_skill 工具需要
        )

        self.max_iterations = max_iterations # Agent Loop 最大迭代次数，防止无限循环

        # ===== Phase 2 新增：构建动态系统提示词 =====
        # 基础提示词 + 技能目录 + 记忆索引，每次 reset 或记忆更新后重建
        self._base_system_prompt = system_prompt  # 保存基础提示词，_build_system_prompt 时拼接
        full_system_prompt = self._build_system_prompt()

        # 对话历史（跨多轮用户输入保持，Agent 能记住之前的操作）
        self.messages: List[Dict] = [
            {"role": "system", "content": full_system_prompt}
        ] # List[Dict] 是类型注解，表示 messages 是一个列表，列表里的每个元素都是字典（Dict），字典里有 role 和 content 两个键值对

        # ===== 改动：计数器全部用 config 常量初始化，不再硬编码 =====
        self._stop_nudge_count = 0                           # 已提醒次数
        self._max_stop_nudges = MAX_STOP_NUDGES             # 从 config 导入（=3），最多提醒3次后放弃
        self.rounds_since_todo = 0                          # nag 计数器
        self._max_rounds_without_todo = TODO_NAG_THRESHOLD  # 从 config 导入（=3），连续N轮没更新todo就提醒
        self._force_tool_call = False                      # nudge 触发后，下一轮强制 tool_choice="required"

        # ===== 新增：Context Compact 上下文压缩器 (对应 s08) =====
        # 在每次调 LLM 前执行四层压缩管道
        self.compactor = ContextCompactor(
            max_messages=COMPACT_MAX_MESSAGES,              # 从 config 导入（=50）
            max_tool_result_chars=COMPACT_MAX_TOOL_RESULT_CHARS, # 从 config 导入（=30000）
        )

        # ===== 改动：stop_nudge_hook 改为闭包，捕获 self，不用 global =====
        # 闭包内的 self 是 FillAgent 实例，可以读写 self._stop_nudge_count
        def stop_nudge_hook(messages: list) -> Optional[str]:
            """
            stop钩子逻辑：
            LLM 没输出 tool_call
            → stop_nudge_hook 检查 todo 列表
                ├─ todo 全部 completed → return None → LLM 正常退出 ✓
                ├─ 没有 todo（单步骤任务）→ return None → LLM 正常退出 ✓
                └─ 有未完成 todo → nudge
                    ├─ 第 1 次：tool_choice="auto"，温和提醒
                    ├─ 第 2 次：tool_choice="required"，强制调工具
                    │  → LLM 被迫调工具 → _force_tool_call=False, count=0
                    │  → 工具执行完，LLM 继续任务
                    └─ 第 3 次：放弃，return None → 退出

            nudge 1：count=0 → count<3 → 提醒，count 变 1
            nudge 2：count=1 → count<3 → 提醒 + force_tool_call=True，count 变 2
            nudge 3：count=2 → count<3 → 提醒 + force_tool_call=True，count 变 3
            nudge 4：count=3 → count>=3 → return None → 放弃，退出循环

            Stop 钩子：当 LLM 没有调用工具时，决定是退出还是注入提醒。

            Qwen2.5 有时候只说不做。但 nudge 前先检查 todo 列表：
            - 有未完成的 todo → 任务没做完 → nudge + 强制 tool_choice
            - 全部完成或没有 todo → 任务做完了 → 放行退出

            返回 None：退出 run()
            返回字符串：注入到 messages 里，继续循环
            """
            # 检查 todo 列表：有未完成项才 nudge
            # 检查 todo 列表：有未完成项才 nudge
            if self.tools._todo_list:
                has_incomplete = any(t.get("status") != "completed" for t in self.tools._todo_list)
                if not has_incomplete:
                    return None  # 所有 todo 都完成了，让 LLM 正常退出

            # 没有 todo list 时，检查最近是否有工具调用（任务进行中）
            if not self.tools._todo_list:
                # 检查最近 10 条消息里有没有 tool result
                recent_msgs = messages[-10:] if len(messages) > 10 else messages
                has_tool_result = any(m.get("role") == "tool" for m in recent_msgs)

                if has_tool_result and self._stop_nudge_count < self._max_stop_nudges:
                    # 任务进行中（有工具调用历史）但 LLM 不调工具了 → nudge
                    self._stop_nudge_count += 1
                    if self._stop_nudge_count >= 2:
                        self._force_tool_call = True
                    return (
                        "你必须立即调用工具执行下一步操作，不要只描述意图。"
                        "如果你正在更新任务清单，请调用 todo_write 工具，不要在文本中列状态。"
                    )
                # 没有近期工具调用（单步骤任务或闲聊），放行
                return None

            # 有未完成的任务，但 LLM 不调工具 → nudge
            if self._stop_nudge_count >= self._max_stop_nudges:
                return None
            self._stop_nudge_count += 1
            if self._stop_nudge_count >= 2:
                self._force_tool_call = True
            return "你必须立即调用工具执行下一步操作，不要只描述意图。如果你正在更新任务清单，请调用 todo_write 工具，不要在文本中列状态。"

        # ===== 注册全部 6 个钩子 (对应 s04/s05) =====
        # 前 5 个是 standalone 函数（不需要访问实例属性）
        # stop_nudge_hook 是闭包（通过闭包捕获 self，访问实例属性）
        register_hook("UserPromptSubmit", context_inject_hook)
        register_hook("PreToolUse", log_hook)         # 先打印日志
        register_hook("PreToolUse", permission_hook)   # 再检查权限
        register_hook("PostToolUse", large_output_hook)
        register_hook("Stop", stop_nudge_hook)         # 再决定是否 nudge（闭包）

    # ========================================================
    # Phase 2 新增方法：动态系统提示词构建 + 记忆提取
    # ========================================================

    def _build_system_prompt(self) -> str:
        """
        构建动态系统提示词（Phase 2 新增，对应 s07 + s09）。

        组装顺序：
        1. 基础系统提示词（config.py 的 SYSTEM_PROMPT，核心规则）
        2. 技能目录（SkillLoader.get_catalog()，每技能约 100 tokens）
        3. 记忆索引（MemoryManager.get_memory_index()，约 200 tokens）

        对比 Phase 1：
        - Phase 1: SYSTEM_PROMPT 是全量硬编码，每次全量发送（约 2000 tokens）
        - Phase 2: SYSTEM_PROMPT 精简为核心规则（约 800 tokens）+ 技能目录（约 400 tokens）
                   + 记忆索引（约 200 tokens），详细规则按需加载

        每次调用时重新读取记忆索引，确保最新的记忆被注入。
        """
        prompt = self._base_system_prompt
        # 注入当前工作目录，让模型导出文件时知道往哪写
        prompt += f"\n\n## 当前工作目录\n导出CSV文件请放在此目录下: {os.getcwd()}\n"
        # 拼接技能目录（启动时扫描，内容不变）
        prompt += self.skill_loader.get_catalog() # 加上技能目录
        # 拼接记忆索引（每次调用时重新读取，包含最新记忆）
        prompt += self.memory.get_memory_index()
        return prompt

    def _extract_memories(self):
        """
        对话结束后提取记忆（Phase 2 新增，对应 s09）。

        流程：
        1. 调用 MemoryManager.extract_memories() 从对话历史中提取记忆
        2. extract_memories 内部已自动调用 _consolidate（超限才合并，否则直接返回）
        3. 如果提取到新记忆，更新系统提示词（注入新的记忆索引）

        调用时机：run() 正常退出时（LLM 返回纯文本，任务完成）
        不在 API 错误或 length 截断时调用（对话不完整）。

        记忆提取使用 LLM 分析对话，识别值得跨会话记忆的内容：
        - 用户偏好（字段映射习惯、常用导出路径）
        - 项目事实（表结构、字段含义）
        - 操作模式（典型查询模式、常见问题处理方式）
        """
        try:
            count = self.memory.extract_memories(self.messages, self.llm)
            if count > 0:
                # extract_memories 内部已自动调用 _consolidate，索引已是最新
                self.messages[0]["content"] = self._build_system_prompt()
        except Exception as e:
            print(f"[Memory] 记忆提取失败: {e}")

    def run(self, user_input: str):
        """
        处理一次用户输入，运行 Agent Loop 直到 LLM 停止调用工具。

        升级后的流程（与 s05 一致）：
        1. 将用户输入添加到对话历史
        2. UserPromptSubmit 钩子（可注入上下文）
        3. 进入循环：
           a. nag 检查（循环顶部，LLM 调用前）           <-- 与 s05 一致
           b. Context Compact（每次调 LLM 前压缩）
           c. 调用 LLM，获取回复（content + tool_calls）
              - API 超限时 reactive_compact 应急压缩
           d. 如果 LLM 没有 tool_calls：
              - 触发 Stop 钩子（nudge）
              - nudge 返回提醒 -> 注入 messages，继续循环
              - nudge 返回 None -> 打印回复，结束
           e. 如果 LLM 返回 tool_calls：
              - rounds_since_todo += 1（与 s05 第 534 行一致）
              - 将 assistant 消息加入历史
              - 逐个执行工具：
                - 用 SimpleNamespace 包装 block（与 s05 第 559 行一致）
                - PreToolUse 钩子（log + permission）
                - 执行工具
                - PostToolUse 钩子（large_output）
                - 如果是 todo_write，重置 rounds_since_todo（与 s05 第 587 行一致）
                - 结果加入历史
              - 回到步骤 a
        4. 如果达到最大迭代次数，停止并提示
        5. 对话结束后提取记忆 (Phase 2 新增，对应 s09)

        Phase 2 新增：
        - 步骤 5：对话结束后调用 _extract_memories()，从对话中提取值得记忆的内容
        - 记忆提取失败不影响主流程（try/except 保护）
        """
        # ===== 改动：删除 global 声明，全部用 self. 实例属性 =====

        # ---- 步骤 1：将用户输入添加到对话历史 ----
        # 不自动清空，保留对话历史，让ask_user等工具的上下文不丢失
        # 用户开始新任务时手动输入 reset 清空
        self._stop_nudge_count = 0   # 每处理一个新用户输入，计数器清零
        self.messages.append({"role": "user", "content": user_input})

        # ---- 步骤 2：UserPromptSubmit 钩子 (s04) ----
        # 可以在这里注入额外上下文（如当前数据库状态）
        # 目前注册了 context_inject_hook，只做日志记录
        prompt_modifier = trigger_hooks("UserPromptSubmit", user_input)
        if prompt_modifier: # 目前看着像多余的，但是拓展后可能会有用
            self.messages.append({"role": "user", "content": prompt_modifier})

        for _ in range(self.max_iterations):
            # ===== 限流保护：每次调 LLM 前等 1 秒，平滑 TPM 消耗 =====
            # GLM-4.5-Air 有 TPM 限制，连续快速调用会触发 429
            time.sleep(1)

            # ---- 步骤 3a：nag 检查（循环顶部，LLM 调用前，与 s05 第 475 行一致）----
            # 连续 N 轮没更新 todo_write 就注入提醒
            if self.rounds_since_todo >= self._max_rounds_without_todo and self.messages: # nag计数器的值大于等于阈值，且 messages 不为空
                self.messages.append({
                    "role": "user",
                    "content": (
                        "<reminder>你必须调用 todo_write 工具来更新任务清单状态，"
                        "不要在文本回复中列出任务状态。"
                        "todo_write 工具的 todos 参数应包含完整的任务列表，"
                        "每项带 content 和 status 字段。</reminder>"
                    )
                })
                self.rounds_since_todo = 0  # 重置，避免重复提醒

            # ---- 步骤 3b：Context Compact (s08) ----
            # ===== 改动（关键 bug 修复）：用临时变量 llm_messages 存压缩结果，不覆盖 self.messages =====
            # self.messages 是完整原始记录（永久账本），compact 返回的是临时副本（只给 LLM 看的精简版）
            # 如果 self.messages = compact(...)，原始历史会被压缩副本永久覆盖，tool result 被替换成占位符后再也回不来
            llm_messages = self.compactor.compact(self.messages, self.llm)

            # ---- 步骤 3c：调用 LLM ----
            # ===== 改动：用 llm_messages（压缩副本）调 LLM，不碰 self.messages =====
            # 如果 nudge 触发了强制模式，用 tool_choice="required" 逼 LLM 输出 tool_call
            tool_choice = "required" if self._force_tool_call else "auto"
            try:
                response = self.llm.chat(llm_messages, self.tools.schemas, tool_choice)
            except Exception as e:
                # ---- Reactive Compact 应急压缩 (s08) ----
                # API 返回 token 超限时，激进压缩 + LLM 摘要，然后重试
                error_msg = str(e).lower()
                if "context" in error_msg or "token" in error_msg or "length" in error_msg:
                    print(f"\n[上下文压缩] 检测到 token 超限，执行应急压缩...")
                    # ===== 改动：同样用临时变量，不覆盖 self.messages =====
                    llm_messages = self.compactor.reactive_compact(self.messages, self.llm)
                    try:
                        response = self.llm.chat(llm_messages, self.tools.schemas, tool_choice)
                    except Exception as e2:
                        print(f"\n[API 调用失败] {e2}")
                        return
                else:
                    print(f"\n[API 调用失败] {e}")
                    return

            finish_reason = response["finish_reason"] # 结束原因
            content = response["content"] # LLM 的文本回复（可能有也可能没有）
            tool_calls = response["tool_calls"] # 工具调用列表（如果 LLM 决定调用工具）

            # 处理输出长度超限，不是你代码里写的限制，是 API 服务端返回的状态：模型输出打到了最大 token 上限，被强制截断了，内容是残缺的。这时候继续往下跑一定会出问题，所以直接return 终止是合理的
            if finish_reason == "length":
                print("\n[警告] LLM 输出达到长度上限被截断，可能是上下文过长或输出内容过多。")
                print("建议：简化问题、或减少对话历史后重试。")
                return

            # ---- 打印 LLM 的思考过程 ----
            if content:
                print(f"\n[Agent] {content}")

            # ---- 步骤 3d：如果没有工具调用 ----
            # 通过 trigger_hooks 调用 Stop 钩子（nudge 再决定是否提醒）
            if not tool_calls:
                # 触发 Stop 钩子，传入 self.messages（与 s05 第 528 行 trigger_hooks("Stop", messages) 一致）
                force = trigger_hooks("Stop", self.messages)
                if force:
                    # nudge 返回提醒，注入到 messages，继续循环
                    self.messages.append({
                        "role": "assistant",
                        "content": content or "",
                    })
                    self.messages.append({
                        "role": "user",
                        "content": force
                    })
                    continue  # 继续循环，不退出
                # nudge 返回 None（提醒次数用完），正常退出
                self.messages.append({
                    "role": "assistant",
                    "content": content or "",
                })
                # ===== Phase 2 新增：对话结束后提取记忆 (s09) =====
                # 只有正常退出（任务完成）才提取记忆，API错误和length截断不提取
                self._extract_memories()
                return # 直接结束run()函数

            # ---- 步骤 3e：LLM 返回了 tool_calls ----
            # 模型恢复调工具了，取消强制模式
            self._force_tool_call = False
            # rounds_since_todo += 1（与 s05 第 534 行一致：一次模型回复无论几个 tool_call，计数器只 +1）
            self.rounds_since_todo += 1

            # 将 assistant 消息（含 tool_calls）加入历史
            # content 可能为 None，转为空字符串以兼容 API 要求
            assistant_msg = {
                "role": "assistant",
                "content": content or "",
                "tool_calls": tool_calls,
            }
            self.messages.append(assistant_msg)

            # 有工具调用时重置 Stop nudge 计数（LLM 在调用工具，不需要 nudge）
            self._stop_nudge_count = 0

            # ---- 逐个执行工具调用 ----
            for tc in tool_calls:
                name = tc["function"]["name"]
                args_str = tc["function"]["arguments"]

                # 解析参数（LLM 返回的 arguments 是 JSON 字符串）
                try:
                    arguments = json.loads(args_str) # 把 JSON 格式的字符串，翻译成 Python 内存字典
                except json.JSONDecodeError:
                    print(f"\n[工具调用] {name}（参数解析失败: {args_str}）")
                    arguments = {}

                # 打印工具调用信息
                args_display = json.dumps(arguments, ensure_ascii=False) # 字典转字符串
                print(f"\n[工具调用] {name}({args_display})")

                # ---- 用 SimpleNamespace 包装 block（与 s05 第 559 行一致）----
                # 适配器：让 hook 代码用 block.name / block.input 访问，不用改
                block = SimpleNamespace(
                    name=name,
                    input=arguments,
                    id=tc["id"],
                    type="tool_use"
                )

                # ---- PreToolUse 钩子：log + 权限检查 (s03/s04) ----
                # trigger_hooks 返回 None 放行，返回字符串拦截
                # 先调 log_hook（打印日志，返回 None），再调 permission_hook（检查权限，可能拦截）
                blocked = trigger_hooks("PreToolUse", block)
                if blocked is not None:
                    # 钩子拦截了工具执行，blocked 字符串作为工具结果返回给 LLM
                    result = blocked
                    print(f"[工具拦截] {result}")
                else:
                    # 执行工具
                    try:
                        result = self.tools.execute(name, arguments) # self.tools.execute()会解包arguments，返回的是字符串
                    except KeyboardInterrupt:
                        print("\n[用户中断] 工具执行被中断。")
                        return

                # ---- PostToolUse 钩子：超大输出告警 (s04) ----
                trigger_hooks("PostToolUse", block, result)

                # ---- s05: 如果调用了 todo_write，重置 nag 计数器（与 s05 第 587 行一致）----
                if name == "todo_write":
                    self.rounds_since_todo = 0

                # 打印结果预览（完整结果已发送给 LLM）
                preview = result[:500]
                if len(result) > 500:
                    preview += "\n...(结果已截断，完整结果已发送给LLM)"
                print(f"[工具结果] {preview}")

                # 将工具结果加入对话历史
                # 这是 Agent Loop 的关键：工具结果作为上下文回传给 LLM，
                # LLM 根据结果决定下一步操作
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        # ---- 达到最大迭代次数 ----
        print(f"\n[警告] 已达到最大迭代次数 ({self.max_iterations})，Agent 停止运行。")
        # ===== Phase 2 新增：即使达到最大迭代次数，也尝试提取记忆 =====
        # 对话虽然未正常完成，但可能包含有用的信息
        self._extract_memories()

    def reset(self):
        """
        清空对话历史（保留系统提示词）。

        Phase 2 改动：reset 后重建系统提示词，包含最新的技能目录和记忆索引。
        这样 Agent 即使清空了对话历史，仍然记得跨会话记忆中的用户偏好和项目事实。
        """
        # 重建系统提示词（包含最新的记忆索引）
        self.messages = [{"role": "system", "content": self._build_system_prompt()}]
        # 重置所有计数器
        self._stop_nudge_count = 0
        self.rounds_since_todo = 0
        self._force_tool_call = False
        print("[对话已重置]（跨会话记忆已保留）")