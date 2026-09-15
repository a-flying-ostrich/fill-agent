---
name: export_guide
description: 导出指南：分别导出、合并导出、CSV编码、文件覆盖确认、字段顺序
---

# 导出指南

## 导出方式选择

导出前，必须使用ask_user询问用户选择导出格式：
- 合并导出：多个表的查询结果合并为一个CSV文件，使用export_merged工具
- 分别导出：每个表单独导出为CSV文件，使用export_result工具

## 分别导出规则

- 每个表用export_result单独导出为CSV文件
- 文件名中包含表名，避免覆盖
- export_result参数：sql（SELECT查询语句）和file_path（导出路径）
- 导出文件使用UTF-8-BOM编码，兼容Excel直接打开中文不乱码

## 文件覆盖确认

- 导出文件已存在时，系统会提示用户确认是否覆盖
- 用户选择不覆盖时，导出操作被取消

## 字段顺序规则

- 导出CSV时，字段顺序必须与用户最初请求中提到的字段顺序保持一致
- 不要擅自调整字段顺序

## 导出前检查

- 先用execute_sql查看部分数据，确认查询结果正确
- 确认无误后再用export_result或export_merged导出
- SQL报错时不要导出，先修正SQL或向用户提问
