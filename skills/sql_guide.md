---
name: sql_guide
description: SQL编写指南：中文表名处理、反引号包裹、字段名引用、安全检查规则
---

# SQL编写指南

## 中文表名和字段名处理

- 表名和字段名可能是中文，编写SQL时请使用list_tables返回的精确表名和字段名
- SQLite语法中，中文表名和字段名建议用反引号(`)包裹
  - 正确写法: SELECT `姓名` FROM `学生信息`
  - 错误写法: SELECT 姓名 FROM 学生信息

## 字段名引用规则

- 编写SQL时，请参考之前import_excel或list_tables返回的精确字段名，不要凭记忆猜测
- 如果SQL报字段不存在，调用list_tables重新确认字段名
- 字段名不完全匹配但语义相近时（如用户要"年级"，表里只有"所在年"），必须用ask_user问用户是否可以替代，不能擅自决定

## 安全检查规则

- 仅允许SELECT语句，不能修改或删除数据
- 不能包含INSERT/UPDATE/DELETE/DROP/ALTER/CREATE等危险关键字
- 如果需要查询多个表，逐个表分别查询和导出，不要写UNION ALL拼接的超长SQL
- 每个表单独export_result导出

## 查询结果处理

- 查询结果可能较大，先用execute_sql查看部分数据（最多显示3行预览），确认无误后再用export_result或export_merged导出
- 如果SQL执行报错且不确定如何修正时，使用ask_user向用户提问
