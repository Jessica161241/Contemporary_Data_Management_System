# 基于 MongoDB 的动态问卷系统（第二阶段）

## 一、系统说明
本项目在原有问卷系统（第一阶段）的基础上进行了架构升级，以满足“题目复用、多版本管理、共享与跨问卷统计”的复杂业务需求。
通过引入独立的**题库系统（Question Bank）**与**快照隔离机制（Snapshot Isolation）**，系统实现了题目模板的版本树演进，同时保证已发布旧问卷的数据绝对安全。

**技术栈**：
- 后端：Python 3 + Flask
- 数据库：MongoDB (文档型数据库)
- 认证：PyJWT (JSON Web Tokens)
- 前端：原生 HTML5 + Vanilla JS + CSS3

### 项目文件结构
```text
Project_1_phase2/
├── app.py                # Flask 后端主程序，包含 API 路由与复杂聚合管道
├── config.py             # 数据库 URI 和 JWT 密钥配置
├── init_db.py            # MongoDB 初始化脚本，包含 Schema Validator 与索引
├── requirements.txt      # 项目依赖清单
├── README.md             # 项目说明文档（本文件）
├── 测试用例.md           # 测试用例与截图记录
├── templates/            # 前端 HTML 页面
│   ├── login.html        # 登录与注册
│   ├── dashboard.html    # 用户控制台（我的问卷）
│   ├── edit.html         # 问卷编辑器（支持快照导入与双轨修改）
│   ├── fill.html         # 问卷填写页
│   ├── stats.html        # 单问卷统计页
│   └── bank.html         # 题库管理页（版本树、共享、跨问卷统计）
└── images/               # 测试截图目录
```

## 二、数据库设计 (MongoDB)
第二阶段在保留第一阶段集合（`users`、`surveys`、`questions`、`responses`）的基础上，新增了两个核心集合，并扩展了字段。

1. **`question_bank` (题库集合)**：存放可复用的题目模板。
   - 核心字段：`root_id` (题目永久逻辑 ID)、`version` (版本号)、`is_latest` (是否为最新版)、`owner_id`、`visibility` (private/shared/public)。
   - 设计理念：通过 `root_id` 将同一道题的多个版本（v1, v2...）绑定成一棵版本树。
2. **`question_bank_shares` (题库共享集合)**：记录题目的分享权限（`root_id`, `user_id`, `role`）。
3. **`question_history` (原生题历史集合)**：记录问卷内纯手敲题目的修改历史（`question_id`, `version`, `old_data`）。
4. **`questions` (问卷题目集合 - 扩展)**：新增了 `bank_root_id`、`bank_version`、`source_bank_version_id` 等字段，使其成为题库题目的**不可变快照**。

## 三、API 说明
所有新增业务接口均位于 `/api/question_bank/` 和扩展的问卷接口下。

**题库管理 API**：
- `POST /api/question_bank`：创建题库题目（生成 v1 版本）。
- `GET /api/question_bank`：获取当前用户创建的及共享给自己的最新版题目。
- `PUT /api/question_bank/<root_id>`：更新题库（自动将旧版本设为 `is_latest=false`，生成 vN+1）。
- `GET /api/question_bank/<root_id>/history`：获取该题的历史版本树。
- `POST /api/question_bank/<root_id>/revert/<version>`：**一键回滚**（将指定旧版本的 `is_latest` 改为 True，不产生新版本）。
- `POST /api/question_bank/<root_id>/share`：共享题目给指定用户。
- `DELETE /api/question_bank/<root_id>`：删除题目（如已被问卷引用，则拦截拒绝）。
- `GET /api/question_bank/<root_id>/usage`：查询该题被哪些问卷引用。
- `GET /api/question_bank/<root_id>/stats`：**跨问卷统计**（按版本隔离，聚合所有关联问卷的数据）。

**问卷题目扩展 API**：
- `POST /api/surveys/<survey_id>/questions/from_bank`：从题库深拷贝快照至问卷。
- `PUT /api/surveys/<survey_id>/questions/<question_id>/bank-link`：手动关联或同步快照与题库。

## 四、关键逻辑说明
1. **快照隔离机制 (Snapshot Isolation)**：
   - 当从题库导入题目时，系统采用**深拷贝**将题目完整内容复制到 `questions` 集合。
   - 无论后续题库中的模板如何升级，问卷中的这道快照题都不会受到影响。这完美满足了需求：*“修改题目时不要影响已经做好的问卷”*。
2. **双轨制修改与分支逻辑**：
   - 在问卷编辑页修改带有 `bank_root_id` 的快照题时，前端会提供三种分支选项：
     1. 仅修改当前问卷快照（不影响题库）。
     2. 同步到题库并生成新版本（vN+1）。
     3. 另存为全新的题库题目（生成新 v1）。
3. **跨问卷统计引擎**：
   - 通过 `root_id` 关联不同问卷里的快照。
   - 在 MongoDB 的 `$facet` 聚合管道中，按 `bank_version` 进行严格分组，避免不同版本的题型差异（如 v1 是数字题，v2 是单选题）导致数据混淆。
   - 实现了版本切换、总引用数、总回答数及各问卷明细的溯源查询。

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
   - `JWT_SECRET_KEY`：用于签发登录 Token 的密钥
   - `JWT_EXPIRES_HOURS`：Token 的有效期（默认 24 小时）

3. **前端 API 地址配置**
   前端页面中的请求地址（`API_BASE`）默认指向 `http://localhost:5000`。若部署到服务器，需在 `templates` 下的所有 HTML 文件中统一修改 `API_BASE` 变量。

4. **数据库初始化 (`init_db.py`)**
   运行 `python init_db.py` 将自动创建第二阶段的全部集合（含 Validator 校验与索引），并清空旧测试数据。