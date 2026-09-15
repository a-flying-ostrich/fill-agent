# 面向多源异构数据的智能填报 Agent 系统

基于 Agent Loop 架构的智能数据填报系统，以 LLM 为决策中枢，集成 9 类工具实现 Excel 导入、SQL 查询、多表合并与导出全流程自动化。系统叠加权限管控、上下文压缩、跨会话记忆等 7 大核心子系统，支撑长程多轮交互场景下的稳定运行。

## 项目架构

```
fill-agent/
│
├── 源码文件
│   ├── main.py              # 程序入口，交互式命令行
│   ├── agent.py             # Agent Loop 核心循环 + Hooks 钩子 + 权限管控 + Subagent
│   ├── config.py            # 全局配置（API Key、模型、数据库路径、系统提示词）
│   ├── llm_client.py        # LLM 客户端封装（限流重试、空 choices 防御检查）
│   ├── excel_importer.py    # Excel 导入引擎（表头评分算法、合并单元格处理、多Sheet导入）
│   ├── context_compact.py   # 四层上下文压缩管道（snip/micro/budget/reactive）
│   ├── memory.py            # 跨会话记忆系统（提取/存储/索引/整理四子系统）
│   └── metadata.py          # 数据库元数据管理（表名/字段/行数记录）
│
├── 技能文件目录（按需加载，Agent 运行时通过 load_skill 工具读取）
│   └── skills/
│       ├── sql_guide.md     # SQL 查询技能（字段确认规则、常见错误处理）
│       ├── export_guide.md  # 导出技能（单表导出流程、路径规范）
│       ├── import_guide.md  # 导入技能（Excel 导入规则、多Sheet处理）
│       └── merge_guide.md   # 多表合并技能（字段对齐、竞赛名称列补充）
│
├── 运行输出文件（代码运行后自动生成，非源码）
│   ├── fill_agent.db        # SQLite 数据库（导入 Excel 后自动建表，存储查询数据）
│   ├── .memory/             # 记忆系统输出目录（对话结束后自动提取并持久化）
│   │   └── *.md             # 各条记忆文件（YAML frontmatter + 正文）
│   ├── *.csv                # 导出结果文件（Agent 按用户需求自动生成）
│   └── *.xlsx               # 输入数据文件（用户提供，Agent 导入后处理）
│
└── README.md                # 项目说明文档
```

> **说明**：`fill_agent.db`、`.memory/`、`*.csv`、`*.xlsx` 均为程序运行时自动生成或用户提供的输入文件，非项目源码。数据库文件可删除后重新导入生成，记忆文件可在新会话中重新积累，CSV 文件为查询导出结果。

## 核心特性

### 1. Agent Loop 核心架构
- 构建 LLM 决策与 Harness 执行的分层循环架构：LLM 自主选择工具并输出调用参数，Harness 执行工具并将结果回传，循环直至任务完成
- 设置 max_iterations 上限（80 轮）与 Stop Hook 双保险防止无限循环
- 工具执行错误以字符串形式返回让 LLM 自行处理，而非抛异常中断

### 2. 四类 Hooks 钩子扩展体系
- `UserPromptSubmit`：输入验证、记忆索引注入
- `PreToolUse`：权限检查（三级安全管线）、日志记录
- `PostToolUse`：超大输出告警、结果格式化
- `Stop`：任务完成检测、Nag Reminder 提醒、tool_choice 强制调用

### 3. 三级权限安全管线
- **第一层 · 硬拒绝**：拦截 DROP/DELETE 等危险 SQL
- **第二层 · 规则匹配**：检测 NULL AS 占位字段、导出路径覆盖确认
- **第三层 · 用户审批**：导入和导出操作弹窗确认

### 4. 四层上下文压缩管道
按「便宜的先跑、贵的后跑」原则设计四级压缩：

| 层级 | 名称 | 机制 | API 调用 |
|------|------|------|----------|
| L1 | snip_compact | 消息超 60 条截断中间轮次 | 0 |
| L2 | micro_compact | 旧工具结果替换为占位符 | 0 |
| L3 | tool_result_budget | 超大结果（>3 万字符）落盘留预览 | 0 |
| L4 | reactive_compact | 应急 LLM 摘要（API 报 413 时触发）| 1 |

前三层纯结构操作零 API 调用，长对话场景下 token 消耗降低 60% 以上。

### 5. 跨会话记忆系统
- **提取**：对话结束后自动调用 LLM 提取三类记忆
  - `preference`：用户偏好（字段映射习惯、常用导出路径、输出格式偏好）
  - `fact`：项目事实（表结构、字段含义、数据规律）
  - `pattern`：操作模式（典型查询模式、常见问题处理方式）
- **存储**：以 Markdown + YAML frontmatter 格式持久化到 `.memory/` 目录
- **索引**：记忆目录注入 SYSTEM_PROMPT 实现跨会话知识积累
- **整理**：记忆超限时（20 条）自动触发 LLM 合并去重

### 6. 两级技能按需加载
- **第一级 · 目录常驻**：启动时扫描 `skills/` 目录，将技能名称与描述（约 100 tokens/技能）注入 SYSTEM_PROMPT
- **第二级 · 全文按需加载**：Agent 需要完整规则时通过 `load_skill` 工具加载全文（约 2000 tokens/技能）
- 相比全量硬编码节省 75% token 消耗

### 7. Subagent 子代理架构
- 主 Agent 遇到复杂子任务时 spawn 子 Agent
- 子 Agent 拥有独立 messages 上下文（隔离），用受限工具集执行
- 完成后只回传最终摘要结论（不回传中间过程），防止子任务上下文污染主对话

### 8. TodoWrite 任务规划与 Nag Reminder
- Agent 接收复杂任务后先分解为带状态（pending/in_progress/completed）的步骤列表
- Nag Reminder：连续 3 轮未更新清单自动注入提醒
- Stop Hook 检查未完成项并配合 `tool_choice='required'` 强制工具调用，防止模型「只说不做」提前退出

### 9. 智能 Excel 导入引擎
- 基于评分算法自动识别表头行（文本占比 50% + 字段完整度 30% + 长度特征 20%）
- 处理合并单元格（左上角值填充）、字段名清洗（正则替换非法字符为下划线）
- 无效数据行过滤（空行/重复表头检测）
- 支持多 Sheet 一次性导入 SQLite 并自动记录元数据

## 快速开始

### 环境要求

- Python 3.10+
- 依赖：`openai`, `pandas`, `openpyxl`

### 安装

```bash
git clone https://github.com/你的用户名/fill-agent.git
cd fill-agent
pip install openai pandas openpyxl
```

### 配置

编辑 `config.py`，填入你的 API Key 和接口地址：

```python
API_KEY = "sk-your-api-key-here"
BASE_URL = "https://api.siliconflow.cn/v1"  # 或本地 vLLM 地址
MODEL_NAME = "Qwen/Qwen2.5-72B-Instruct"
```

### 运行

```bash
python main.py
```

### 使用示例

```
你: 处理2024年学科科技竞赛学生获奖统计表.xlsx，收集电信学部的获奖学生、项目、指导老师、学号和年级
```

系统会自动：
1. 导入 Excel（自动识别多个 Sheet，建表并记录元数据）
2. 查询各表电信学部数据（字段不匹配时主动询问用户）
3. 询问导出方式（合并为一个文件 or 分别导出）
4. 导出 CSV 到当前目录

运行结束后，`.memory/` 目录会自动保存本次对话中提取的记忆，下次启动时自动加载。

## 部署模式

| 模式 | 配置 | 适用场景 |
|------|------|----------|
| API 调用 | `BASE_URL` 指向云端 API（如 SiliconFlow） | 快速验证、无 GPU 环境 |
| 本地 vLLM | `BASE_URL` 指向本地 vLLM 服务 | 数据隐私、低延迟、免 API 费用 |

切换部署模式只需修改 `config.py` 中的三个值：`API_KEY`、`BASE_URL`、`MODEL_NAME`。

## 技术栈

- **LLM**: Qwen2.5-72B-Instruct / GLM-4.5-Air（支持 Function Calling）
- **接口**: OpenAI 兼容接口（`client.chat.completions.create`）
- **数据**: Pandas + SQLite + openpyxl
- **部署**: vLLM（本地） / SiliconFlow API（云端）

## License

MIT
