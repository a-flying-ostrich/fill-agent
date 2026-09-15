---
name: merge_guide
description: 合并导出指南：AS别名统一字段、NULL占位、output_fields顺序、竞赛名称列
---

# 合并导出指南

## export_merged工具参数

- tables_queries: 每个表的查询配置列表，每项包含table_name和sql
- output_fields: 用户要求的输出字段顺序，如 ["获奖学生", "项目", "指导老师", "学号", "年级"]
- file_path: 导出CSV文件的完整路径

## AS别名规则（关键）

使用export_merged时，每个表的SQL必须用AS别名将字段名统一为output_fields中的名称：

1. 表里有该字段但名字不同的，用AS重命名
   - 例如: `项目名称` AS `项目`
   - 例如: `指导教师` AS `指导老师`

2. 表里没有该字段的，用NULL AS占位
   - 例如: NULL AS `项目`

3. 字段名完全匹配的，也建议用AS显式标注
   - 例如: `学号` AS `学号`

## output_fields顺序

- output_fields参数使用用户最初请求的字段顺序
- 每个表的SQL返回的列名（AS别名后）必须与此列表对应
- 导出的CSV文件表头顺序: 竞赛名称 + output_fields

## 竞赛名称列

- 每行数据自动添加"竞赛名称"列，值为该行数据来源的table_name
- 竞赛名称列始终在第一列

## 示例

用户要求导出字段: ["获奖学生", "项目", "指导老师", "学号", "年级"]

表"美国大学生数学建模竞赛"有字段: 获奖学生、项目名称、指导教师、学号、年级

该表的SQL应写为:
```sql
SELECT `获奖学生` AS `获奖学生`,
       `项目名称` AS `项目`,
       `指导教师` AS `指导老师`,
       `学号` AS `学号`,
       `年级` AS `年级`
FROM `美国大学生数学建模竞赛`
WHERE `学院` = '电信学部'
```
