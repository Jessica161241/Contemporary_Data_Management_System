# 当代数据管理系统 (Contemporary Data Management System) - 课程作业仓库

本仓库用于存放《当代数据管理系统》课程的所有大作业代码与文档。

**学生信息**：
- 姓名：单馨冉
- 课程：当代数据管理系统
- 学期：2026 秋季学期

---

## 📚 作业目录

### [Project 1：基于 MongoDB 的问卷系统开发](./Project_1)
- **阶段**：第一阶段
- **技术栈**：Python Flask + MongoDB + 原生 HTML/JS/CSS
- **核心功能**：
  - 用户注册、登录与 JWT 鉴权。
  - 问卷创建、状态管理（草稿/发布/关闭）。
  - 支持单选、多选、文本、数字四种题型。
  - **核心亮点**：基于 JSON 的动态跳转逻辑（`routing_rules`），支持防死循环检测。
  - **数据统计**：利用 MongoDB 聚合管道（Aggregation Pipeline）实现高效统计。
- **详细说明**：请查看 [Project 1 的 README 文档](./Project_1/README.md)。
- **测试报告**：请查看 [Project 1 测试用例与结果](./Project_1/测试用例.md)。


