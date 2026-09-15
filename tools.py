"""
定义 Agent 可用的工具及其 JSON Schema(给大模型看的工具调用描述说明书)：
1. import_excel  — 导入 Excel 文件到数据库
2. list_tables   — 列出数据库中所有表及字段
3. execute_sql   — 执行 SELECT 查询（安全检查在 agent.py 的 permission_hook 中）
4. export_result — 导出查询结果为 CSV
5. ask_user      — 向用户提问
6. todo_write    — 更新任务清单（对应 s05 TodoWrite）
7. load_skill    — 按需加载技能全文（对应 s07 Skill Loading）
8. task          — 派生子Agent处理复杂子任务（对应 s06 Subagent）

每个工具返回字符串，作为 tool message 的 content 回传给 LLM。
LLM 根据返回结果决定下一步操作。

升级内容（Phase 1）：
- 新增 todo_write 工具 (s05)，让 Agent 先列计划再执行

升级内容（Phase 2）：
- 新增 load_skill 工具 (s07)，Agent 按需加载技能全文
- 新增 task 工具 (s06)，主Agent派生子Agent处理复杂子任务
- ToolManager 新增 llm_client、skill_loader、restricted 参数
"""

'''注注注：
所有工具函数的入参，全部来自大模型 Function‑Calling 的输出，不是我们代码手动填的。
但要分清两件事：
1. 参数值：由大模型生成
2. 参数长什么样（Schema）：是我们人提前写好规定给大模型看的
还有：所有 tool 工具 return 出来的值，首要接收方是 LLM，不是打印到终端给人看

## 完整链路
1. 我们人写 JSON Schema，告诉大模型：
有个工具叫record_table，它需要参数table_name(字符串)、columns(数组)…… 必须按这个格式输出。

2. LLM 根据对话上下文，自己推理、生成每一个参数的具体内容。
比如：
{
  "name": "record_table",
  "arguments": {
    "table_name": "sales_2025",
    "columns": ["id","price","dt"],
    "row_count": 1200
  }
}
👉 table_name、columns、row_count 的具体内容全部是大模型输出。

3. Agent 代码拿到这一段 JSON，解析出来，反射调用你写的 python 工具函数，把模型输出的值传进去。
self.record_table(
    table_name="sales_2025",
    columns=["id","price","dt"],
    row_count=1200
)

例外：哪些不是大模型给的？
1. 工具自身的 self 参数：类实例，Python 自动注入，和大模型无关。
2. 代码内部硬编码的值，比如你函数内部写死的常量，不是入参。
3. 工具返回结果：是 Python 函数运行出来的结果，不是大模型输出，这个结果再塞回消息历史喂给大模型。
'''

import csv
import sqlite3
from typing import List, Dict
from excel_importer import ExcelImporter
from metadata import MetadataManager
from config import SUBAGENT_MAX_ITERATIONS

# ===== 工具 JSON Schema 定义 =====
# 遵循 OpenAI Function Calling 格式
# LLM 根据这些 schema 决定调用哪个工具、传什么参数

TOOL_SCHEMAS: List[Dict] = [
    {
        "type": "function", # OpenAI 规范强制要求：标识这一条是函数工具定义，固定写法，不用改动。
        "function": {
            "name": "import_excel",
            "description": "将Excel文件导入数据库。支持多Sheet工作簿，自动处理合并单元格和表头识别。返回导入的表名和字段信息。",
            "parameters": {
                "type": "object", # 代表所有参数打包成一个对象（字典），告诉大模型：调用这个工具时，parameters 这一整块，必须是一个 JSON 对象（大括号 {}），不能是字符串、数组、数字。比如：{"file_path": "C:/data/example.xlsx"}
                "properties": { # 列出所有可用参数
                    "file_path": {
                        "type": "string",
                        "description": "Excel文件的完整路径，例如 C:/data/example.xlsx",
                    }
                },
                "required": ["file_path"], #调用 import_excel 工具必须提供 file_path 参数，不允许空着调用
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "列出数据库中所有表的名称、字段和数据行数。用于了解数据库中有哪些数据表可供查询。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": "执行SQL SELECT查询并返回结果。仅允许SELECT语句，不能修改数据。结果最多显示50行，如需全部数据请使用export_result导出。",
            "parameters": {
                "type": "object",
                "properties": { # 列出所有可用参数
                    "sql": {
                        "type": "string",
                        "description": "要执行的SQL SELECT查询语句",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_result",
            "description": "将SQL查询结果导出为CSV文件。会执行指定的SQL查询并将全部结果保存到文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "要导出结果的SQL SELECT查询语句",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "导出文件的完整路径，例如 C:/output/result.csv",
                    },
                },
                "required": ["sql", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_merged",
            "description": "将多个表的查询结果合并导出为一个CSV文件，每行自动添加竞赛名称列。输出字段顺序固定，保证与用户需求一致。当用户选择合并导出时使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tables_queries": {
                        "type": "array",
                        "description": "每个表的查询配置列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "table_name": {
                                    "type": "string",
                                    "description": "表名（也是竞赛名称列的值）"
                                },
                                "sql": {
                                    "type": "string",
                                    "description": "该表的SELECT查询语句。必须用AS别名将字段名统一为output_fields中的名称。表里有该字段但名字不同的，用AS重命名（如`项目名称` AS `项目`）。表里没有该字段的，用NULL AS占位（如NULL AS `项目`）。"
                                }
                            }
                        }
                    },
                    "output_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "用户要求的输出字段顺序，如 [\"获奖学生\", \"项目\", \"指导老师\", \"学号\", \"年级\"]"
                    },
                    "file_path": {
                        "type": "string",
                        "description": "导出文件的完整路径，例如 C:/output/result.csv"
                    }
                },
                "required": ["tables_queries", "output_fields", "file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "向用户提问以获取必要的信息或确认。当用户的请求不够明确、需要补充信息时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "要向用户提出的问题",
                    }
                },
                "required": ["question"],
            },
        },
    },
    # ===== Phase 1 新增：TodoWrite 工具 (对应 s05) =====
    # 让 Agent 先列出步骤清单，再逐步执行
    # 每完成一步更新状态，避免 LLM 中途跑偏
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "更新任务清单。在执行多步骤任务前（如导入多个Excel、逐表查询后合并导出），"
                "先用此工具列出所有步骤并标记状态。每完成一步更新对应步骤的状态为completed。"
                "状态说明：pending=待执行，in_progress=正在执行，completed=已完成。"
                "单步骤任务无需使用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array", # 数组，在python里也就是列表
                        "description": "任务清单列表，包含所有步骤及其当前状态",
                        "items": {
                            "type": "object", # 数组里的每一项都是一个对象（字典），在python里也就是字典
                            "properties": { # 必填两个参数content和status
                                "content": {
                                    "type": "string",
                                    "description": "任务步骤的描述，如：导入学生信息Excel文件"
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "该步骤的当前状态：pending(待执行)、in_progress(正在执行)、completed(已完成)"
                                }
                            },
                            "required": ["content", "status"]
                        }
                    }
                },
                "required": ["todos"]
            }
        }
    },
    # ===== Phase 2 新增：load_skill 工具 (对应 s07 Skill Loading) =====
    # Agent 按需加载技能全文，启动时只注入技能目录（便宜），用到时才加载全文（贵）
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "加载技能的完整内容。当需要详细的操作指南（如SQL编写规则、导出规则、合并导出规则）时，"
                "先查看系统提示中的技能目录了解有哪些技能可用，然后调用此工具加载对应技能的全文。"
                "技能全文包含详细的操作步骤、示例和注意事项。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "要加载的技能名称，如 sql_guide、export_guide、import_guide、merge_guide",
                    }
                },
                "required": ["skill_name"],
            },
        },
    },
    # ===== Phase 2 新增：task 工具 (对应 s06 Subagent) =====
    # 主Agent派生子Agent处理复杂子任务，子Agent有独立的对话上下文
    # 子Agent的工具集更小（不含task工具，防止递归），30轮安全限制
    {
        "type": "function",
        "function": {
            "name": "task",
            "description": (
                "派生子Agent处理复杂子任务。子Agent有独立的对话上下文，完成后只返回最终摘要。"
                "适合需要多步SQL调试的复杂查询任务。子Agent的中间对话历史不会污染主Agent的上下文。"
                "使用场景：某个表的查询需要多次调试SQL、字段映射复杂、需要独立分析的子任务。"
                "不要对简单任务使用此工具，直接用execute_sql等工具处理。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "子任务的详细描述，包括要查询的表名、需要的字段、查询条件、导出要求等。描述越详细，子Agent执行效果越好。",
                    }
                },
                "required": ["description"],
            },
        },
    },
]
'''
todo_write 工具 Schema 整体是正确的 ✅
## 解析 Schema 结构
- 工具名：todo_write
- 用途：多步骤任务维护待办清单，单步骤任务不调用
- 参数：todos 是数组 (array)
  - 数组中每一个元素是对象object
  - 每个对象必填两个字段：
    - content(string)：步骤文字描述
    - status(string)，只能三枚举值：pending / in_progress / completed
事例：
{
  "todos": [
    {
      "content": "导入竞赛数据Excel文件",
      "status": "in_progress"
    },
    {
      "content": "读取数据库所有表元信息",
      "status": "pending"
    },
    {
      "content": "多表查询并且合并导出CSV结果",
      "status": "pending"
    }
  ]
}
'''

'''大模型输出工具调用时，arguments 就必须是这个 object ：
{
"tool_calls": [
    {
    "function": {
        "name": "import_excel",
        "arguments": {       // ← 这里就是 type:object 对应的对象
            "file_path": "C:/data/example.xlsx"
        }
    }
    }
]
}
'''

# ===== SQL 安全检查已移至 agent.py =====
# _is_safe_sql 仅在 agent.py 的 permission_hook 中调用，tools.py 内部不使用
# 集中检查所有执行 SQL 的工具，tools.py 内部不再重复检查

# 子Agent不可用的工具列表（防止递归派生 + 避免不必要的高级功能）
# restricted=True 的 ToolManager 会过滤掉这些工具
_RESTRICTED_TOOLS = ["task", "load_skill", "todo_write"]

class ToolManager:
    """
    工具管理器：统一管理所有工具的定义和执行。

    Agent Loop 通过 execute() 方法调用工具，
    通过 schemas 属性获取工具定义传给 LLM。

    Phase 2 新增参数：
    - llm_client:  LLMClient 实例，task 工具创建子Agent时需要
    - skill_loader: SkillLoader 实例，load_skill 工具加载技能时需要
    - restricted:   是否为受限模式（子Agent用），过滤掉 task/load_skill/todo_write 工具
    """

    def __init__(self, db_path: str, llm_client=None, skill_loader=None, restricted: bool = False):
        """
        初始化工具管理器。

        Args:
            db_path:     SQLite 数据库文件路径
            llm_client:  LLMClient 实例（task 工具创建子Agent时需要，Phase 2 新增）
            skill_loader: SkillLoader 实例（load_skill 工具加载技能时需要，Phase 2 新增）
            restricted:  是否为受限模式（子Agent用，过滤掉 task/load_skill/todo_write，Phase 2 新增）
        """
        self.db_path = db_path
        self.llm_client = llm_client         # task 工具需要（Phase 2 新增）
        self.skill_loader = skill_loader     # load_skill 工具需要（Phase 2 新增）

        # 任务清单状态（对应 s05 TodoWrite）
        # Agent 通过 todo_write 工具更新此列表，run() 中的 nag 计数器会读取它
        self._todo_list: List[Dict] = []

        # 工具名 -> 处理函数的映射，所有工具函数的入参，全部来自大模型 Function‑Calling 的输出，不是我们代码手动填的
        self._handlers = {
            "import_excel": self._tool_import_excel,
            "list_tables": self._tool_list_tables,
            "execute_sql": self._tool_execute_sql,
            "export_result": self._tool_export_result,
            "export_merged": self._tool_export_merged,
            "ask_user": self._tool_ask_user,
            "todo_write": self._tool_todo_write,       # Phase 1 新增 (s05)
            "load_skill": self._tool_load_skill,       # Phase 2 新增 (s07)
            "task": self._tool_task,                    # Phase 2 新增 (s06)
        }

        # ===== Phase 2 新增：受限模式过滤 =====
        # 子Agent使用受限模式，移除 task/load_skill/todo_write 工具
        # task 工具移除：防止子Agent递归派生子Agent
        # load_skill 移除：子Agent不需要技能加载，保持简单
        # todo_write 移除：子Agent处理单一任务，不需要任务清单
        if restricted:
            for tool_name in _RESTRICTED_TOOLS:
                self._handlers.pop(tool_name, None)

        # 构建 schema 列表（受限模式下过滤掉不可用的工具）
        if restricted:
            self.schemas = [
                s for s in TOOL_SCHEMAS
                if s["function"]["name"] not in _RESTRICTED_TOOLS
            ]
        else:
            self.schemas = TOOL_SCHEMAS # 这个参数在agent.py里面调用了

    def execute(self, name: str, arguments: Dict) -> str:
        """
        执行指定工具，返回结果字符串。

        所有异常在此捕获，返回错误信息字符串（而非抛出异常），
        这样 LLM 可以看到错误并决定如何处理（如重试或换一种方式）。

        Args:
            name:      工具名称
            arguments: 工具参数字典

        Returns:
            工具执行结果（字符串），一定是字符串
        """
        handler = self._handlers.get(name)
        if not handler:
            return f"错误: 未知工具 '{name}'，可用工具: {list(self._handlers.keys())}"

        try:
            return handler(**arguments) # 字典解包，会把字典里的每一对 key:value，拆成**关键字实参**传给函数
        except TypeError as e:
            return f"错误: 工具参数不匹配 - {e}"
        except Exception as e:
            return f"错误: 工具执行失败 - {e}"

    # ========================================================
    # 工具实现：所有工具函数的入参，全部来自大模型 Function‑Calling 的输出，不是我们代码手动填的
    # ========================================================

    def _tool_import_excel(self, file_path: str) -> str: # Excel 文件路径不是写死在代码里的，必须由用户输入给到大模型
        """
        工具：导入 Excel 文件到数据库。

        调用 ExcelImporter 完成实际导入，将结构化结果格式化为LLM 可读的文本。
        """
        importer = ExcelImporter(self.db_path)
        result = importer.import_excel(file_path)

        if not result["success"]:
            return f"导入失败: {result.get('error', '未知错误')}"

        lines = [f"导入成功，共创建 {len(result['tables'])} 个表："] # lines 是一个字符串列表，里面每一项是一个字符串，最终会用 "\n".join(lines) 拼接成一个大字符串返回给 LLM
        for table in result["tables"]: # result["tables"]是一个字典列表，里面每一项是一个字典对象，包含表名、原始Sheet名、字段列表、数据行数等信息
            lines.append(f"\n表名: {table['table_name']}")
            lines.append(f"  原始Sheet名: {table['original_sheet_name']}")
            lines.append(f"  字段: {', '.join(table['columns'])}")
            lines.append(f"  数据行数: {table['row_count']}")

        if result["skipped_sheets"]:
            lines.append(f"\n跳过的Sheet: {', '.join(result['skipped_sheets'])}")

        return "\n".join(lines) # 将字符串列表 lines 拼接成一个大字符串返回给 LLM，作为工具调用的结果

    def _tool_list_tables(self) -> str:
        """
        工具：列出数据库中所有表及字段信息。

        调用 MetadataManager 获取所有表元数据，格式化为
        LLM 可读的文本。LLM 根据这些信息语义判断哪些表相关。
        """
        manager = MetadataManager(self.db_path)
        tables = manager.list_all_tables() # 返回的是是字典列表，每个字典包括每张表的元数据：表名、原始Sheet名、字段列表、数据行数、源文件路径、创建时间等

        if not tables:
            return "数据库中暂无表。请先使用 import_excel 工具导入Excel文件。"

        lines = [f"数据库中共有 {len(tables)} 个表："]
        for table in tables:
            lines.append(f"\n表名: {table['table_name']}")
            lines.append(f"  原始Sheet名: {table['original_sheet_name']}")
            lines.append(f"  字段: {', '.join(table['columns'])}")
            lines.append(f"  数据行数: {table['row_count']}")

        return "\n".join(lines) # 同上

    def _tool_execute_sql(self, sql: str) -> str:
        """
        工具：执行 SQL SELECT 查询。

        安全检查 -> 执行查询 -> 格式化结果（最多 50 行）。
        大量数据时提示 LLM 使用 export_result 导出。
        """

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [desc[0] for desc in cursor.description] # 取出本次 SQL 查询返回结果的列名
            '''cursor.description的用法
            比如SELECT name, age FROM user，那么cur.description为(
            ('name', None, None, None, None, None, None),
            ('age', None, None, None, None, None, None))
            cursor.description它是一个元组组成的序列。每一个小元组，对应查询结果里的一列。格式固定：(列名, 类型码, 显示大小, 内部大小, 精度, 比例, 是否接受null)
            '''
            # 一次性拿到所有数据值，rows是一个元组列表，每个元组对应一行数据，元组里的每一项对应一列的值，没有列名
            rows = cursor.fetchall()
        except sqlite3.Error as e:
            return f"SQL执行错误: {e}"
        finally:
            conn.close()

        if not rows:
            return "查询结果为空。"

        MAX_DISPLAY = 3
        lines = [f"查询结果（共 {len(rows)} 行，显示前 {min(len(rows), MAX_DISPLAY)} 行）："]
        lines.append(" | ".join(columns)) # 返回的是一个拼接完成的字符串
        lines.append("-" * 40)
        for row in rows[:MAX_DISPLAY]:
            lines.append(" | ".join((str(v) if v is not None else "") for v in row))
            '''
            如果单元格值是None（数据库里的 NULL） → 替换成空字符串""，其他值 → str(v)转字符串
            row = ["张三", None, 22]
            # 生成器产出： "张三" , "" , "22"
            并没有删掉分隔符！空内容还在，分隔符|照样保留

            还有：
            if 后面带 else → 三元表达式，改每个元素，写在 for 前面：A if 条件 else B，这是 Python 内置的三元运算符，它本身就是一个值
            if 后面没有 else → 过滤条件，筛掉元素，写在 for 后面，格式：`[值 for x in 可迭代对象 if 条件]`
            '''

        if len(rows) > MAX_DISPLAY:
            lines.append(
                f"\n... 还有 {len(rows) - MAX_DISPLAY} 行未显示。"
                f"如需全部数据，请使用 export_result 工具导出。"
            )
        return "\n".join(lines) # 返回的是一个很大很大的字符串，里面包含了查询结果的表头（列名）、数据行、提示信息等，最终会作为工具调用的结果返回给 LLM

    def _tool_export_result(self, sql: str, file_path: str) -> str:
        """
        工具：将 SQL 查询结果导出为 CSV 文件。

        安全检查 -> 执行查询 -> 写入 CSV（UTF-8-BOM 编码，
        兼容 Excel 直接打开中文不乱码）。
        """

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [desc[0] for desc in cursor.description] # 取出本次 SQL 查询返回结果的列名
            # 一次性拿到所有数据值，rows是一个元组列表，每个元组对应一行数据，元组里的每一项对应一列的值，没有列名
            rows = cursor.fetchall()
        except sqlite3.Error as e:
            return f"SQL执行错误: {e}"
        finally:
            conn.close()

        try: # 把查询出来的列名 + 数据行，导出保存成 CSV 文件
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                '''
                with 上下文管理器：打开文件，代码块结束自动帮你关闭文件，不用手动写 f.close()。
                "w"：write 写入模式。如果文件不存在就新建；如果文件已存在，直接覆盖旧内容。
                newline="" ✨csv 库专属关键参数
                '''
                writer = csv.writer(f)
                writer.writerow(columns) # writerow（单数）：写 1 行
                writer.writerows(rows) # writerows（复数，带 s）：一次性写多行
        except IOError as e:
            return f"文件写入失败: {e}"

        return f"已导出 {len(rows)} 行数据到 {file_path}"

    def _tool_ask_user(self, question: str) -> str:
        """
        工具：向用户提问。
        question 这个入参是大模型输出出来的问题，LLM 需要用户回答时调用此工具。

        当 LLM 需要补充信息时调用此工具。
        通过 input() 获取用户回答，将回答返回给 LLM。
        """
        print(f"\n[Agent提问] {question}")
        response = input("你的回答: ").strip()
        return response if response else "用户未输入内容"

    def _tool_export_merged(self, tables_queries: List[Dict], output_fields: List[str], file_path: str) -> str:
        """
        工具：将多个表的查询结果合并导出为一个CSV文件。
        LLM在SQL中用AS别名将字段名统一为output_fields中的名称。
        缺失字段用NULL AS 字段名 占位。

        Args:
            tables_queries: 每个表的查询配置列表，每项是一个字典，包含:
                - table_name: 表名（字符串），也是"竞赛名称"列的值，标记每行数据来自哪个表
                - sql: 该表的SELECT查询语句（字符串），必须用AS别名将字段名统一为output_fields中的名称
            output_fields: 用户要求的输出字段顺序（字符串列表），如 ["获奖学生", "项目", "指导老师", "学号", "年级"]
                决定CSV的列表头顺序，每个表的SQL返回的列名（AS别名后）必须与此列表对应
            file_path: 导出CSV文件的完整路径（字符串），如 "C:/output/result.csv"

        Returns:
            成功时返回 "已合并导出 N 行数据到 {file_path}"（字符串），N为所有表合计的行数
            失败时返回错误信息字符串，如 "表 {table_name} 查询失败: {错误详情}"
        """
        all_rows = []
        for item in tables_queries:
            table_name = item["table_name"]
            sql = item["sql"]

            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.cursor()
                cursor.execute(sql)
                columns = [desc[0] for desc in cursor.description]
                '''
                假设这个表的 SQL 是：
                SELECT `获奖学生` AS `获奖学生`, NULL AS `项目`, `指导教师` AS `指导老师`, `学号` AS `学号`, `年级` AS `年级`
                FROM `美国大学生数学建模竞赛`
                WHERE `学院` = '电信学部'
                执行后 cursor.description 返回的 columns 是：
                [获奖学生, 项目, 指导老师, 学号, 年级]
                '''
                rows = cursor.fetchall()
            except sqlite3.Error as e:
                conn.close()
                return f"表 {table_name} 查询失败: {e}"
            finally:
                conn.close()

            # 构建列名到索引的映射（此时列名是AS别名后的统一名称）
            col_index = {col: i for i, col in enumerate(columns)}

            for row in rows:
                # 按 output_fields 顺序取值
                merged_row = []
                for field in output_fields:
                    if field in col_index:
                        val = row[col_index[field]]
                        merged_row.append(str(val) if val is not None else "")
                    else:
                        merged_row.append("")
                # 最前面加竞赛名称
                merged_row.insert(0, table_name)
                all_rows.append(merged_row)

        # 写入CSV
        header = ["竞赛名称"] + output_fields
        try:
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(header)
                writer.writerows(all_rows)
        except IOError as e:
            return f"文件写入失败: {e}"

        return f"已合并导出 {len(all_rows)} 行数据到 {file_path}"

    # ========================================================
    # Phase 1 新增：TodoWrite 工具实现 (对应 s05)
    # ========================================================

    def _tool_todo_write(self, todos: List[Dict]) -> str:
        """
        工具：更新任务清单。

        Agent 在执行多步骤任务前调用此工具列出步骤清单，
        每完成一步更新状态。agent.py 中的 nag 计数器会检测
        Agent 是否长时间未更新清单，如果超过阈值会注入提醒。

        设计要点（与 s05 一致）：
        - todos 参数是完整的清单（不是增量更新），每次调用覆盖整个列表
        - 状态用 pending/in_progress/completed 三种
        - print 格式化清单给用户看（终端进度面板）
        - return 简短确认给 LLM 看（避免冗余信息回传）

        Args:
            todos: 任务清单列表，每项包含 content(描述) 和 status(状态)

        Returns:
            简短确认字符串（给 LLM 看）
        """
        self._todo_list = todos  # 覆盖更新整个清单

        # 状态符号映射，让输出更直观
        status_marks = {
            "pending": "○",       # 待执行
            "in_progress": "◐",   # 正在执行
            "completed": "●",     # 已完成
        }

        # 1. print 格式化清单 → 给用户看（终端进度面板）
        lines = ["\n## 任务清单"]
        for i, todo in enumerate(todos, 1):
            status = todo.get("status", "pending")
            content = todo.get("content", "")
            mark = status_marks.get(status, "○")
            lines.append(f"  {mark} {i}. {content} [{status}]")

        total = len(todos)
        completed = sum(1 for t in todos if t.get("status") == "completed")
        lines.append(f"\n进度: {completed}/{total} 已完成")
        print("\n".join(lines))

        # 2. return 简短确认 → 给 LLM 看
        return f"任务清单已更新，共 {total} 项，{completed} 项已完成。"

    # ========================================================
    # Phase 2 新增：load_skill 工具实现 (对应 s07 Skill Loading)
    # ========================================================

    def _tool_load_skill(self, skill_name: str) -> str:
        """
        工具：按需加载技能全文。即便他后面被L2上下文读过后压缩了，需要时再加载一次即可。
        关键理解：技能加载后的那几轮，LLM 已经"读完"并"吸收"了技能内容到后续的 reasoning 里。等它被 L2 替换掉的时候，LLM 已经在用它了。如果过了很多轮又需要，再调一次 load_skill 就行——这正是"按需加载"的含义。

        Agent 在需要详细操作指南时调用此工具。
        启动时系统提示中只注入了技能目录（名称+描述，约100 tokens/技能），
        调用此工具后加载完整技能内容（约2000 tokens/技能）。

        两层加载机制（与 s07 一致）：
        - 第一层：技能目录每轮注入 SYSTEM（便宜，约100 tokens/技能）
        - 第二层：技能全文按需加载（贵，约2000 tokens/技能），只有 LLM 主动调用时才花 token

        设计要点：
        - skill_loader 在 ToolManager.__init__ 时传入
        - 如果 skill_loader 未配置，返回提示信息
        - 返回的技能全文作为 tool_result 回传给 LLM，LLM 根据全文中的规则执行操作

        Args:
            skill_name: 技能名称（如 "sql_guide"、"export_guide"）

        Returns:
            技能全文文本，技能不存在时返回错误提示
        """
        if not self.skill_loader:
            return "错误: 技能加载器未配置，无法加载技能。"

        content = self.skill_loader.load_skill(skill_name)

        if content.startswith("错误:"):
            return content

        print(f"\n[Skill] 已加载技能: {skill_name} ({len(content)} 字符)")
        return content

    # ========================================================
    # Phase 2 新增：task 工具实现 (对应 s06 Subagent)
    # ========================================================

    def _tool_task(self, description: str) -> str:
        """
        工具：派生子Agent处理复杂子任务。

        主Agent遇到需要多步SQL调试的复杂子任务时调用此工具。
        子Agent拥有独立的对话上下文，完成后只返回最终摘要。

        设计要点（与 s06 一致）：
        - 子Agent有独立的 messages 列表（上下文隔离）
        - 子Agent工具集更小（不含task/load_skill/todo_write，防止递归）
        - 30轮安全限制（防止无限循环）
        - 完成后只返回最终文本摘要，中间历史丢弃
        - 主Agent的上下文不被子任务的SQL调试细节污染

        子Agent的创建流程：
        1. 创建受限的 ToolManager（restricted=True，过滤掉 task/load_skill/todo_write）
        2. 创建 Subagent 实例（共享 LLM 客户端，传入受限工具集）
        3. 运行 Subagent，获取最终摘要
        4. 摘要作为 tool_result 回传给主Agent

        适合场景：
        - 某个表的查询需要多次调试SQL
        - 字段映射复杂，需要多次尝试
        - 需要独立分析的子任务

        Args:
            description: 子任务的详细描述，包括表名、字段、条件、导出要求等

        Returns:
            子Agent的最终文本摘要
        """
        if not self.llm_client:
            return "错误: LLM客户端未配置，无法创建子Agent。"

        # ---- 延迟导入，避免循环依赖 ----
        # subagent.py 的 Subagent 不需要导入 tools.py（接收预配置的 ToolManager）
        # 但 tools.py 的 _tool_task 需要导入 subagent.py 的 Subagent
        # 延迟导入（在方法内部 import）打破循环依赖
        from subagent import Subagent

        # ---- 创建受限的 ToolManager（子Agent专用）----
        # restricted=True 过滤掉 task/load_skill/todo_write
        # 子Agent不需要 llm_client 和 skill_loader（它不创建子Agent，不加载技能）
        restricted_tools = ToolManager(
            self.db_path,
            restricted=True,
        ) # 同一个 Python 进程里新建另一个独立的 ToolManager 实例，不是递归调用当前正在执行的这个 ToolManager 对象，两个实例是完全独立的两个对象。
        '''
        你现在正在跑的：主Agent的ToolManager实例A（带 task、load_skill、todo_write）
        在_tool_task内部新建：子Agent的ToolManager实例B（restricted=True，阉割掉三个高级工具）
        A 和 B 是两个不同对象，内存地址不一样，互不干扰

        ## 递归为什么不会发生？
        - 主 Agent（实例 A）有task工具，可以调用_tool_task
        - 子 Agent（实例 B）的_handlers被 pop 掉了 task，它的schemas也过滤掉 task 定义，子 Agent 的 LLM 根本看不到 task 这个工具
        - 子 Agent 没有任何办法再调用task，自然无法再去创建 “孙 Agent”，递归链条直接切断
        '''

        # ---- 创建子Agent ----
        # 共享 LLM 客户端（同一个 API 连接，不需要新建）
        # 传入受限工具集
        sub = Subagent(
            llm_client=self.llm_client,
            tool_manager=restricted_tools,
            max_iterations=SUBAGENT_MAX_ITERATIONS,
        )

        # ---- 运行子Agent ----
        # 子Agent完成后只返回最终文本摘要
        # 中间对话历史（messages）随 Subagent 实例销毁而丢弃
        # 主Agent的 self.messages 不受影响（上下文隔离）
        result = sub.run(description)

        return result