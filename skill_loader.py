"""
技能加载模块 (对应 learn-claude-code s07)

两层加载机制（核心设计）：
1. 启动时扫描 skills/ 目录，构建 SKILL_REGISTRY（只读标题和描述，注入 SYSTEM，便宜）
   - 每技能约 100 tokens，4 个技能共约 400 tokens
2. Agent 需要完整规则时调 load_skill 工具展开全文（约 2000 tokens，贵）
   - 按需加载，不提前注入，显著降低 token 消耗

设计原则（与 s07 一致）：
- SYSTEM_PROMPT 从全量硬编码改为动态组装
- 技能目录（标题 + 一行描述）每轮注入 SYSTEM，让 LLM 知道有哪些技能可用
- 技能全文（详细规则、示例代码）只在 LLM 主动调用 load_skill 时才加载
- 长对话场景下，大部分轮次不需要加载技能全文，token 消耗大幅降低

技能文件格式：
    ---
    name: sql_guide
    description: SQL编写指南：中文表名处理、反引号包裹、字段名引用规则
    ---

    # SQL编写指南
    ...详细内容...

YAML frontmatter 里的 name 和 description 在启动时解析（便宜），
frontmatter 下方的正文在 load_skill 调用时才读取（贵）。
"""

import os
from typing import Dict, List, Optional


class SkillLoader:
    """
    技能加载器：管理技能的注册、目录生成和按需加载。

    使用方式：
        loader = SkillLoader("skills")
        catalog = loader.get_catalog()        # 获取目录文本，注入 SYSTEM
        content = loader.load_skill("sql_guide")  # 按需加载技能全文
    """

    def __init__(self, skills_dir: str):
        """
        初始化技能加载器，扫描技能目录构建注册表。

        Args:
            skills_dir: 技能文件所在目录路径（如 "skills"）
        """
        self.skills_dir = skills_dir
        # SKILL_REGISTRY: 技能名 -> 元数据字典
        # 元数据包含: name, description, file_path（不包含 content，延迟加载）
        self.registry: Dict[str, Dict] = {}
        self._scan_skills() # 初始化这个一运行，self.registry里面就填满值了

    def _scan_skills(self):
        """
        扫描技能目录，解析每个 .md 文件的 YAML frontmatter，
        构建 SKILL_REGISTRY。

        只读取 name 和 description（约 100 tokens/技能），
        不读取正文内容（约 2000 tokens/技能），实现延迟加载。
        """
        if not os.path.exists(self.skills_dir):
            print(f"[SkillLoader] 技能目录不存在: {self.skills_dir}")
            return

        for filename in os.listdir(self.skills_dir):
            if not filename.endswith(".md"):
                continue

            file_path = os.path.join(self.skills_dir, filename)
            metadata = self._parse_frontmatter(file_path)

            if metadata:
                name = metadata.get("name", filename.replace(".md", ""))
                description = metadata.get("description", "")
                self.registry[name] = {
                    "name": name,
                    "description": description,
                    "file_path": file_path,
                }

        print(f"[SkillLoader] 已加载 {len(self.registry)} 个技能: {list(self.registry.keys())}")

    def _parse_frontmatter(self, file_path: str) -> Optional[Dict]:
        """
        解析 Markdown 文件的 YAML frontmatter（只解析头部元数据，不读正文）。

        YAML frontmatter 格式：
            ---
            name: sql_guide
            description: SQL编写指南
            ---

        使用简单的行解析，不依赖 PyYAML（减少依赖）。

        Args:
            file_path: 技能文件路径

        Returns:
            包含 name 和 description 的字典，解析失败返回 None
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except IOError as e:
            print(f"[SkillLoader] 读取技能文件失败: {file_path}, {e}")
            return None

        # 检查是否以 --- 开头（YAML frontmatter 标记）
        if not content.strip().startswith("---"):
            return None

        # 提取两个 --- 之间的内容
        lines = content.split("\n")
        if lines[0].strip() != "---":
            return None

        # 找到第二个 ---
        end_index = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_index = i
                break

        if end_index is None:
            return None

        # 解析 YAML 键值对（简单解析，不支持嵌套）
        metadata = {}
        for line in lines[1:end_index]:
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                metadata[key] = value

        return metadata

    def get_catalog(self) -> str:
        """
        生成技能目录文本，注入 SYSTEM_PROMPT。

        每个技能只占一行（名称 + 描述），非常便宜（约 100 tokens/技能）。
        LLM 看到目录后，知道有哪些技能可用，需要时调 load_skill 加载全文。

        Returns:
            技能目录文本，格式如：
            可用技能（需要详细规则时调用 load_skill 加载）：
            - sql_guide: SQL编写指南：中文表名处理...
            - export_guide: 导出指南：分别导出、合并导出...
        """
        if not self.registry:
            return ""

        lines = ["\n## 可用技能（需要详细规则时调用 load_skill 工具加载全文）"]
        for name, info in self.registry.items():
            lines.append(f"- {name}: {info['description']}")

        return "\n".join(lines)

    def load_skill(self, skill_name: str) -> str:
        """
        按需加载技能全文（约 2000 tokens，贵）。

        LLM 通过 load_skill 工具调用此方法，获取技能的完整内容。
        启动时不加载全文，只在需要时才读取，实现延迟加载。

        Args:
            skill_name: 技能名称（如 "sql_guide"）

        Returns:
            技能全文文本，技能不存在时返回错误信息
        """
        if skill_name not in self.registry:
            available = list(self.registry.keys())
            return f"错误: 未知技能 '{skill_name}'，可用技能: {available}"

        file_path = self.registry[skill_name]["file_path"]

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except IOError as e:
            return f"错误: 读取技能文件失败: {e}"

        # 去掉 YAML frontmatter，只返回正文
        # frontmatter 格式: ---\n...\n---\n正文
        lines = content.split("\n")
        if lines[0].strip() == "---":
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    # 返回 frontmatter 之后的正文
                    return "\n".join(lines[i + 1:]).strip()

        return content.strip()

    def list_skills(self) -> List[str]:
        """
        返回所有已注册的技能名称列表。

        Returns:
            技能名称列表
        """
        return list(self.registry.keys())
