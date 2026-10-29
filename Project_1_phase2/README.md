
# 基于 MongoDB 的动态问卷系统

## 一、系统说明
本项目是一个基于 Flask 和 MongoDB 开发的轻量级在线问卷系统，类似“问卷星”或 Google Forms。
系统支持多用户注册登录、问卷创建与发布、复杂的动态题目跳转、前后端双端校验，以及答卷结果的聚合统计。前端采用原生 HTML/JS/CSS 编写，轻量且无需复杂的构建工具即可直接运行。

**技术栈**：
- 后端：Python 3 + Flask
- 数据库：MongoDB (文档型数据库)
- 认证：PyJWT (JSON Web Tokens)
- 前端：原生 HTML5 + Vanilla JS + CSS3

### 项目文件结构
```text
Project_1/
├── app.py                # Flask 后端主程序，包含 API 路由、校验引擎和跳转推导逻辑
├── config.py             # 数据库 URI 和 JWT 密钥配置
├── init_db.py            # MongoDB 初始化脚本，包含 Schema Validator 与索引创建
├── requirements.txt      # 项目依赖清单
├── README.md             # 项目说明文档（本文件）
├── 测试用例.md           # 测试用例与测试结果截图记录
├── templates/            # 前端 HTML 页面（原生 HTML + Vanilla JS）
│   ├── login.html        # 登录与注册页
│   ├── dashboard.html    # 用户控制台，展示问卷列表、创建及发布
│   ├── edit.html         # 问卷编辑器，支持动态添加题目、配置校验与跳转规则
│   ├── fill.html         # 问卷填写页，包含前端动态路径推导与平滑滚动反馈
│   └── stats.html        # 问卷统计页，展示选项分布、文本列表与数值平均值
└── images/               # 测试用例截图存放目录
```

## 二、数据库设计 (MongoDB)
系统采用 MongoDB 作为唯一数据存储方案，充分发挥了文档数据库灵活扩展的优势。系统包含 4 个核心集合（Collection）：
1. **users**：存储用户信息（用户名唯一索引、密码哈希、注册时间）。
2. **surveys**：存储问卷信息（标题、说明、状态 draft/published/closed、截止时间、创建者 ID）。
3. **questions**：存储题目，采用 `config` 嵌套文档兼容单选、多选、文本、数字四种题型。**跳转规则 `routing_rules` 以 JSON 数组形式原生存储**。
4. **responses**：存储答卷，`answers` 字段利用数组存储动态答案，并记录 `visited_question_ids`。

**为什么选择 MongoDB？**
关系型数据库处理“动态题型配置”和“复杂跳转规则”需要设计极多的关联表，且修改扩展极其困难。MongoDB 的文档模型允许我们将题目的配置项（如选项列表、限制范围）和跳转规则作为嵌套文档直接存储，读写高效，且完美支持系统在第二阶段的扩展需求。

## 三、API 说明
所有的业务接口均位于 `/api/` 路径下，核心接口如下：
- `POST /api/register`：用户注册，密码使用 scrypt 加盐哈希。
- `POST /api/login`：用户登录，返回 JWT Token。
- `POST /api/surveys`：创建问卷（需鉴权）。
- `POST /api/surveys/<survey_id>/questions`：为问卷添加题目（包含跳转配置，需鉴权）。
- `GET /api/surveys/<survey_id>/fill`：获取用于填写的问卷（支持草稿预览）。
- `POST /api/surveys/<survey_id>/submit`：提交答卷，进行严格的后端规则校验。
- `GET /api/surveys/<survey_id>/stats`：获取聚合统计结果（需鉴权）。

## 四、关键逻辑说明
1. **动态跳转逻辑（Routing Rules）**：
   题目跳转不写死在代码中，而是通过前端的可视化配置，在题库中存入 `routing_rules`。填写时，前端通过 JS 实时比对用户的答案，动态计算路径（隐藏无关题目）。提交时，根据 `visited_question_ids` 记录实际访问路径。
2. **后端防御与校验引擎**：
   为了防作弊与死循环，后端实现了 `validate_answers` 引擎。它会校验所有被访问题目的答案合法性（包括必答、数字范围、字数限制等），并通过 `detect_routing_cycle` 检测路径是否有环，确保数据绝对安全。
3. **统计聚合**：
   使用 MongoDB 的 `$facet`、`$unwind` 和 `$group` 等高级聚合管道，在一次数据库查询中同时完成选项计数、文本收集和数字平均值计算，性能优异。

## 五、如何运行
1. 安装依赖：`pip install -r requirements.txt`
2. 确保本地 MongoDB 运行在 `27017` 端口。
3. 初始化数据库：`python init_db.py`（会清空并重建集合）。
4. 启动服务：`python app.py`
5. 浏览器访问：`http://127.0.0.1:5000/login.html`

## 六、项目配置说明

本系统的配置分离在 `config.py` 和 `requirements.txt` 中，具体说明如下：

1. **环境依赖 (`requirements.txt`)**
   系统依赖 Flask、PyMongo、PyJWT、Werkzeug 和 Flask-CORS。安装命令：
   `pip install -r requirements.txt`

2. **数据库连接配置 (`config.py`)**
   系统默认连接本地 MongoDB 的 `survey_db`，也可通过环境变量覆盖：
   - `MONGO_URI`：MongoDB 连接地址（默认 `mongodb://localhost:27017/survey_db`）
   - `JWT_SECRET_KEY`：用于签发登录 Token 的密钥（建议在生产环境中通过环境变量注入）
   - `JWT_EXPIRES_HOURS`：Token 的有效期（默认 24 小时）

3. **前端 API 地址配置**
   前端页面中的请求地址（`API_BASE`）默认指向 `http://localhost:5000`。如果部署到服务器，需在 `login.html`、`dashboard.html`、`edit.html`、`fill.html`、`stats.html` 中统一修改 `API_BASE` 变量。