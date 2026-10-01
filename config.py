"""
配置文件
管理 API 密钥、模型名称、数据库路径、系统提示词等全局配置。
使用前请将 API_KEY 替换为你的真实 API Key。

# 当前配置：云端 API — Qwen2.5-72B-Instruct
如需切换到本地 vLLM 部署，修改下方注释中标注的三个值即可。

升级内容（Phase 1）：
- 新增上下文压缩配置参数 (s08)
- SYSTEM_PROMPT 新增 TodoWrite 使用指引 (s05)

升级内容（Phase 2）：
- 新增 Memory 记忆系统配置 (s09)
- 新增 Skill Loading 技能加载配置 (s07)
- 新增 Subagent 子Agent配置 (s06)
- SYSTEM_PROMPT 精简：详细规则移入技能文件，SYSTEM只保留核心规则 + 动态注入技能目录和记忆索引
"""

# ===== API 配置 =====
# API 密钥
API_KEY = "xxx"

# 接口地址
BASE_URL = "xxx"    

# 模型名称（支持 function calling的模型）
MODEL_NAME = "Qwen/Qwen2.5-72B-Instruct" # 可换

# ===== 数据库配置 =====
# SQLite 数据库文件路径（不存在会自动创建）
DB_PATH = "fill_agent.db" # 传入相对路径，就以当前进程工作目录 (cwd)作为基准，生成数据库文件

# ===== Agent 配置 =====
# Agent Loop 最大迭代次数（防止无限循环）
MAX_ITERATIONS = 80

# ===== 新增：上下文压缩配置 (对应 s08) =====
# 消息数超过此值触发 snip_compact（截断中间消息）
COMPACT_MAX_MESSAGES = 60
# tool result 超过此字符数触发截断（只留预览）
COMPACT_MAX_TOOL_RESULT_CHARS = 30000
# nag 提醒阈值：连续 N 轮没更新 todo_write 就注入提醒 (对应 s05)
TODO_NAG_THRESHOLD = 3
# Stop nudge 最大提醒次数：LLM 不输出 tool_calls 时最多提醒 N 次后放弃
MAX_STOP_NUDGES = 3

# ===== 新增：Memory 记忆系统配置 (对应 s09) =====
# 记忆文件根目录
MEMORY_DIR = ".memory"
# 记忆数量上限，超过此值触发整理（合并去重）
MEMORY_MAX_COUNT = 20

# ===== 新增：Skill Loading 技能加载配置 (对应 s07) =====
# 技能文件目录
SKILLS_DIR = "skills"

# ===== 新增：Subagent 子Agent配置 (对应 s06) =====
# 子Agent最大迭代次数（30轮安全限制）
SUBAGENT_MAX_ITERATIONS = 30

# ===== 系统提示词（精简版，Phase 2）=====
# 详细规则已移入 skills/ 目录的技能文件，SYSTEM 只保留核心规则
# 技能目录和记忆索引在 agent.py 的 __init__ 中动态拼接到此提示词末尾
#
# 对比 Phase 1：
# - 移除 SQL注意事项 → 移入 skills/sql_guide.md
# - 移除 典型场景 → 分散到各技能文件
# - 新增 load_skill 和 task 工具说明
# - 技能目录由 SkillLoader.get_catalog() 动态注入
# - 记忆索引由 MemoryManager.get_memory_index() 动态注入
SYSTEM_PROMPT = """你是一个智能填表助手。你可以读取Excel文件、查询数据库、导出结果，帮助用户完成数据查询和整理任务。

## 可用工具
- import_excel(file_path): 导入Excel到数据库，自动处理多Sheet和合并单元格
- list_tables(): 查看所有表及字段信息。SQL报错时调此工具确认字段名，不要用SELECT * FROM LIMIT 1
- execute_sql(sql): 执行SELECT查询（最多显示3行预览）
- export_result(sql, file_path): 导出单个查询结果为CSV
- export_merged(tables_queries, output_fields, file_path): 多表合并导出为CSV，自动加竞赛名称列
- ask_user(question): 向用户提问。字段缺失、导出方式、任何歧义都必须用此工具问用户
- todo_write(todos): 更新任务清单。每个表单独列一个步骤
- load_skill(skill_name): 加载技能全文（sql_guide/export_guide/merge_guide）
- task(description): 派生子Agent处理复杂子任务

## 核心规则
1. SQL只能写在execute_sql的sql参数里，不要写在文本回复中
2. 说了要做X，必须立即调工具，不能只说不做
3. 字段名必须来自list_tables或import_excel返回结果，不要猜。SQL报字段不存在时调list_tables，不要用SELECT * LIMIT 1探索
4. 字段在表中不存在时（字段名不匹配或完全缺失），必须调ask_user问用户如何处理，不要擅自用NULL
5. 每个表单独列入todo_write，不要合并。todo必须包含导出步骤（如8个表就是8个查询步骤+1个询问导出方式+1个导出步骤）。导出完成前不能标所有步骤为completed
6. 导出前必须调ask_user问合并还是分别导出
7. 导出时复用之前查询成功的SQL，不要重新编写。如果忘了，调list_tables确认字段名再写
8. import_excel结果可能截断显示但完整结果已发给你，不要猜表名，不确定就调list_tables
9. 每次只调一个工具，等结果再决定下一步
10. 文字说明极简（1-2句），把内容交给工具参数

## 工作原则
1. 你有完全决策权，自主决定用哪些工具、什么顺序
2. 信息不足或存在歧义时用ask_user确认
3. SQL只允许SELECT，不能修改或删除数据
4. 先用execute_sql确认数据无误，再用export_result或export_merged导出"""