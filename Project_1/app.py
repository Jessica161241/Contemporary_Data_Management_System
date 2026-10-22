from datetime import datetime, timedelta, timezone
from functools import wraps
import math

import jwt
from bson import ObjectId
from bson.errors import InvalidId
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config


app = Flask(__name__)
app.config.from_object(Config)
CORS(app)

mongo_client = MongoClient(
    app.config["MONGO_URI"],
    serverSelectionTimeoutMS=3000,
)
database = mongo_client.get_default_database()
users_collection = database["users"]
surveys_collection = database["surveys"]
questions_collection = database["questions"]
responses_collection = database["responses"]


def get_users_collection():
    """Check MongoDB availability and return the users collection."""
    mongo_client.admin.command("ping")
    users_collection.create_index(
        [("username", ASCENDING)],
        unique=True,
        name="uq_users_username",
    )
    return users_collection


def error_response(message, status_code):
    return jsonify({"error": message}), status_code


def token_required(view):
    """Require a valid Bearer JWT and expose its user ID to the endpoint."""

    @wraps(view)
    def decorated(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return error_response(
                "请在 Authorization Header 中提供 Bearer Token",
                401,
            )

        try:
            payload = jwt.decode(
                token,
                app.config["JWT_SECRET_KEY"],
                algorithms=["HS256"],
            )
            token_user_id = payload.get("user_id") or payload.get("sub")
            if not token_user_id:
                raise KeyError("user_id")
            user_id = ObjectId(token_user_id)
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, KeyError, InvalidId):
            return error_response("Token 无效或已过期", 401)

        request.current_user_id = user_id
        return view(*args, **kwargs)

    return decorated


def decode_bearer_token(authorization):
    """Decode an Authorization header and return the authenticated user ID."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ValueError("invalid authorization header")

    payload = jwt.decode(
        token,
        app.config["JWT_SECRET_KEY"],
        algorithms=["HS256"],
    )
    token_user_id = payload.get("user_id") or payload.get("sub")
    if not token_user_id:
        raise KeyError("user_id")
    return ObjectId(token_user_id)


def serialize_survey(survey):
    """Convert MongoDB-specific values into JSON-compatible values."""
    return {
        "id": str(survey["_id"]),
        "title": survey["title"],
        "description": survey["description"],
        "allow_anonymous": survey["allow_anonymous"],
        "status": survey["status"],
        "deadline": survey.get("deadline").isoformat()
        if survey.get("deadline") is not None
        else None,
        "creator_id": str(survey["creator_id"]),
        "created_at": survey["created_at"].isoformat(),
        "updated_at": survey["updated_at"].isoformat(),
    }


def serialize_question(question):
    routing_rules = []
    for rule in question.get("routing_rules", []):
        serialized_rule = dict(rule)
        serialized_rule["conditions"] = []
        for condition in rule.get("conditions", []):
            serialized_condition = dict(condition)
            condition_question_id = serialized_condition.get("question_id")
            if isinstance(condition_question_id, ObjectId):
                serialized_condition["question_id"] = str(condition_question_id)
            serialized_rule["conditions"].append(serialized_condition)
        action = dict(rule.get("action", {}))
        action_target = action.get("target_question_id")
        if isinstance(action_target, ObjectId):
            action["target_question_id"] = str(action_target)
        serialized_rule["action"] = action
        routing_rules.append(serialized_rule)
    return {
        "id": str(question["_id"]),
        "survey_id": str(question["survey_id"]),
        "order": question["order"],
        "title": question["title"],
        "question_type": question["question_type"],
        "is_required": question["is_required"],
        "config": question.get("config", {}),
        "routing_rules": routing_rules,
    }


@app.get("/login.html")
def login_page():
    return render_template("login.html")


@app.get("/dashboard.html")
def dashboard_page():
    return render_template("dashboard.html")


@app.get("/fill.html")
def fill_page():
    return render_template("fill.html")


@app.get("/stats.html")
def stats_page():
    return render_template("stats.html")


@app.get("/edit.html")
def edit_page():
    return render_template("edit.html")


def _is_missing_answer(value):
    return value is None or value == "" or value == []


def _option_values(question):
    options = question.get("config", {}).get("options", [])
    return [
        option.get("value")
        for option in options
        if isinstance(option, dict) and "value" in option
    ]


def validate_answers(questions, answers, visited_question_ids):
    """Validate submitted answers against the stored question definitions."""
    questions_by_id = {str(question["_id"]): question for question in questions}
    visited_set = set(visited_question_ids)
    answers_by_id = {}
    errors = []

    for answer in answers:
        if not isinstance(answer, dict):
            errors.append("每个答案都必须是对象")
            continue

        question_id = answer.get("question_id")
        if not isinstance(question_id, str) or question_id not in questions_by_id:
            errors.append(f"题目不存在：{question_id}")
            continue
        if question_id not in visited_set:
            errors.append(f"答案对应的题目未出现在访问路径中：{question_id}")
            continue
        if question_id in answers_by_id:
            errors.append(f"题目不能重复提交答案：{question_id}")
            continue
        answers_by_id[question_id] = answer.get("value")

    for question_id in visited_set:
        question = questions_by_id[question_id]
        value = answers_by_id.get(question_id)
        question_type = question.get("question_type")
        config = question.get("config", {})

        if question.get("is_required") and _is_missing_answer(value):
            errors.append(f"必答题未作答：{question_id}")
            continue
        if question_id not in answers_by_id or _is_missing_answer(value):
            continue

        if question_type == "single_choice":
            if value not in _option_values(question):
                errors.append(f"单选题选项非法：{question_id}")
        elif question_type == "multiple_choice":
            if not isinstance(value, list):
                errors.append(f"多选题答案必须是数组：{question_id}")
                continue
            seen_values = []
            has_duplicate = False
            for selected_value in value:
                if selected_value in seen_values:
                    has_duplicate = True
                    break
                seen_values.append(selected_value)
            if has_duplicate:
                errors.append(f"多选题不能包含重复选项：{question_id}")
            if any(option not in _option_values(question) for option in value):
                errors.append(f"多选题包含非法选项：{question_id}")
            min_selected = config.get("min_selected")
            max_selected = config.get("max_selected")
            if min_selected is not None and len(value) < min_selected:
                errors.append(f"多选题至少选择 {min_selected} 项：{question_id}")
            if max_selected is not None and len(value) > max_selected:
                errors.append(f"多选题最多选择 {max_selected} 项：{question_id}")
        elif question_type == "text":
            if not isinstance(value, str):
                errors.append(f"文本题答案必须是字符串：{question_id}")
                continue
            min_length = config.get("min_length")
            max_length = config.get("max_length")
            if min_length is not None and len(value) < min_length:
                errors.append(f"文本长度少于限制：{question_id}")
            if max_length is not None and len(value) > max_length:
                errors.append(f"文本长度超过限制：{question_id}")
        elif question_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"数字题答案必须是数字：{question_id}")
                continue
            if not math.isfinite(value):
                errors.append(f"数字题答案必须是有限数字：{question_id}")
            if config.get("is_integer", config.get("integer_only", False)) and not isinstance(value, int):
                errors.append(f"数字题答案必须是整数：{question_id}")
            min_value = config.get("min_value")
            max_value = config.get("max_value")
            if min_value is not None and value < min_value:
                errors.append(f"数字小于最小值：{question_id}")
            if max_value is not None and value > max_value:
                errors.append(f"数字大于最大值：{question_id}")
        else:
            errors.append(f"题目类型不受支持：{question_id}")

    return errors, answers_by_id


def _condition_matches(condition, answers_by_id):
    question_id = str(condition.get("question_id"))
    actual = answers_by_id.get(question_id)
    operator = condition.get("operator")
    expected = condition.get("value")

    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "contains":
        return isinstance(actual, (list, str)) and expected in actual
    if operator == "between":
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False
        lower_ok = actual >= condition.get("min_value") if condition.get("include_min", True) else actual > condition.get("min_value")
        upper_ok = actual <= condition.get("max_value") if condition.get("include_max", True) else actual < condition.get("max_value")
        return lower_ok and upper_ok
    if operator == "greater_than":
        return isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual > expected
    if operator == "less_than":
        return isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual < expected
    return False


def detect_routing_cycle(questions, answers_by_id):
    """Follow stored rules from the first question and reject a cyclic path."""
    ordered_questions = sorted(questions, key=lambda question: question["order"])
    questions_by_id = {str(question["_id"]): question for question in ordered_questions}
    if not ordered_questions:
        return None

    current_id = str(ordered_questions[0]["_id"])
    visited = set()
    while current_id is not None:
        if current_id in visited:
            return f"跳转规则形成循环：{current_id}"
        if current_id not in questions_by_id:
            return f"跳转目标不存在：{current_id}"
        visited.add(current_id)
        current = questions_by_id[current_id]
        next_id = None
        for rule in sorted(current.get("routing_rules", []), key=lambda item: item.get("priority", 0)):
            conditions = rule.get("conditions", [])
            if isinstance(conditions, list) and all(
                _condition_matches(condition, answers_by_id) for condition in conditions
            ):
                action = rule.get("action", {})
                action_type = action.get("type")
                if action_type == "end":
                    return None
                if action_type == "goto":
                    target_question_id = action.get("target_question_id")
                    if target_question_id == "__NEXT__":
                        next_id = None
                    elif target_question_id is not None:
                        next_id = str(target_question_id)
                elif action_type == "skip":
                    skipped = set(action.get("question_ids", []))
                    next_id = next(
                        (
                            str(question["_id"])
                            for question in ordered_questions
                            if question["order"] > current["order"]
                            and str(question["_id"]) not in skipped
                        ),
                        None,
                    )
                break
        if next_id is None:
            next_id = next(
                (
                    str(question["_id"])
                    for question in ordered_questions
                    if question["order"] > current["order"]
                ),
                None,
            )
        current_id = next_id
    return None


@app.post("/api/register")
def register():
    payload = request.get_json(silent=True) or {}
    username = payload.get("username")
    password = payload.get("password")

    if not isinstance(username, str) or not isinstance(password, str):
        return error_response("username 和 password 必须提供且为字符串", 400)

    username = username.strip().casefold()
    if not username or len(username) < 3 or len(username) > 50:
        return error_response("用户名长度必须为 3 至 50 个字符", 400)
    if len(password) < 8:
        return error_response("密码长度不能少于 8 个字符", 400)

    try:
        users = get_users_collection()
        user = {
            "username": username,
            "password_hash": generate_password_hash(password, method="scrypt"),
            "created_at": datetime.now(timezone.utc),
        }
        result = users.insert_one(user)
        return jsonify(
            {
                "message": "注册成功",
                "user_id": str(result.inserted_id),
            }
        ), 201
    except DuplicateKeyError:
        return error_response("用户名已存在", 409)
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during registration")
        return error_response("数据库暂时不可用", 503)


@app.post("/api/login")
def login():
    payload = request.get_json(silent=True) or {}
    username = payload.get("username")
    password = payload.get("password")

    if not isinstance(username, str) or not isinstance(password, str):
        return error_response("username 和 password 必须提供且为字符串", 400)

    username = username.strip().casefold()
    if not username or not password:
        return error_response("用户名和密码不能为空", 400)

    try:
        users = get_users_collection()
        user = users.find_one({"username": username})
        if user is None or not check_password_hash(
            user["password_hash"], password
        ):
            return error_response("用户名或密码错误", 401)

        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": str(user["_id"]),
                "user_id": str(user["_id"]),
                "username": user["username"],
                "iat": now,
                "exp": now + timedelta(hours=app.config["JWT_EXPIRES_HOURS"]),
            },
            app.config["JWT_SECRET_KEY"],
            algorithm="HS256",
        )
        return jsonify(
            {
                "message": "登录成功",
                "token": token,
                "expires_in": app.config["JWT_EXPIRES_HOURS"] * 3600,
            }
        ), 200
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during login")
        return error_response("数据库暂时不可用", 503)


@app.post("/api/surveys")
@token_required
def create_survey():
    payload = request.get_json(silent=True) or {}
    title = payload.get("title")
    description = payload.get("description", "")
    allow_anonymous = payload.get("allow_anonymous", False)
    deadline_value = payload.get("deadline")

    if not isinstance(title, str) or not title.strip():
        return error_response("title 必须是非空字符串", 400)
    if not isinstance(description, str):
        return error_response("description 必须是字符串", 400)
    if not isinstance(allow_anonymous, bool):
        return error_response("allow_anonymous 必须是布尔值", 400)
    if deadline_value is not None and not isinstance(deadline_value, str):
        return error_response("deadline 必须是 ISO 格式的时间字符串", 400)

    deadline = None
    if deadline_value:
        try:
            deadline = datetime.fromisoformat(deadline_value.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline <= datetime.now(timezone.utc):
                return error_response("截止时间必须晚于当前时间", 400)
        except ValueError:
            return error_response("deadline 时间格式无效", 400)

    now = datetime.now(timezone.utc)
    survey = {
        "title": title.strip(),
        "description": description,
        "allow_anonymous": allow_anonymous,
        "status": "draft",
        "deadline": deadline,
        "creator_id": request.current_user_id,
        "created_at": now,
        "updated_at": now,
    }

    try:
        mongo_client.admin.command("ping")
        surveys_collection.create_index(
            [("creator_id", ASCENDING), ("created_at", -1)],
            name="idx_surveys_creator_created",
        )
        result = surveys_collection.insert_one(survey)
        return jsonify(
            {
                "message": "问卷创建成功",
                "survey_id": str(result.inserted_id),
            }
        ), 201
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during survey creation")
        return error_response("数据库暂时不可用", 503)


@app.get("/api/surveys")
@token_required
def list_surveys():
    try:
        mongo_client.admin.command("ping")
        surveys = surveys_collection.find(
            {"creator_id": request.current_user_id}
        ).sort("created_at", -1)
        return jsonify({"surveys": [serialize_survey(survey) for survey in surveys]})
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during survey listing")
        return error_response("数据库暂时不可用", 503)


@app.delete("/api/surveys/<survey_id>")
@token_required
def delete_survey(survey_id):
    try:
        survey_object_id = ObjectId(survey_id)
    except InvalidId:
        return error_response("survey_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        survey = surveys_collection.find_one(
            {
                "_id": survey_object_id,
                "creator_id": request.current_user_id,
            }
        )
        if survey is None:
            return error_response("问卷不存在或无权删除", 404)
        if survey.get("status") != "draft":
            return error_response("只有草稿问卷可以删除", 400)

        surveys_collection.delete_one({"_id": survey_object_id})
        questions_collection.delete_many({"survey_id": survey_object_id})
        return jsonify(
            {
                "message": "问卷删除成功",
                "survey_id": str(survey_object_id),
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during survey deletion")
        return error_response("数据库暂时不可用", 503)


@app.put("/api/surveys/<survey_id>/publish")
@token_required
def publish_survey(survey_id):
    try:
        survey_object_id = ObjectId(survey_id)
    except InvalidId:
        return error_response("survey_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        result = surveys_collection.update_one(
            {
                "_id": survey_object_id,
                "creator_id": request.current_user_id,
            },
            {
                "$set": {
                    "status": "published",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        if result.matched_count == 0:
            return error_response("问卷不存在或无权发布", 404)
        return jsonify(
            {
                "message": "问卷发布成功",
                "survey_id": str(survey_object_id),
                "status": "published",
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during survey publishing")
        return error_response("数据库暂时不可用", 503)


@app.put("/api/surveys/<survey_id>/close")
@token_required
def close_survey(survey_id):
    try:
        survey_object_id = ObjectId(survey_id)
    except InvalidId:
        return error_response("survey_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        result = surveys_collection.update_one(
            {
                "_id": survey_object_id,
                "creator_id": request.current_user_id,
            },
            {
                "$set": {
                    "status": "closed",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        if result.matched_count == 0:
            return error_response("问卷不存在或无权关闭", 404)
        return jsonify(
            {
                "message": "问卷已关闭",
                "survey_id": str(survey_object_id),
                "status": "closed",
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during survey closing")
        return error_response("数据库暂时不可用", 503)


@app.get("/api/surveys/<survey_id>/stats")
@token_required
def survey_stats(survey_id):
    try:
        survey_object_id = ObjectId(survey_id)
    except InvalidId:
        return error_response("survey_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        survey = surveys_collection.find_one(
            {
                "_id": survey_object_id,
                "creator_id": request.current_user_id,
            }
        )
        if survey is None:
            return error_response("问卷不存在或无权查看统计结果", 404)

        questions_collection.create_index(
            [("survey_id", ASCENDING), ("order", ASCENDING)],
            name="idx_questions_survey_order",
        )
        responses_collection.create_index(
            [("survey_id", ASCENDING)],
            name="idx_responses_survey",
        )

        question_list = list(
            questions_collection.find({"survey_id": survey_object_id}).sort(
                "order", ASCENDING
            )
        )
        total_result = list(
            responses_collection.aggregate(
                [
                    {"$match": {"survey_id": survey_object_id}},
                    {"$count": "total"},
                ]
            )
        )
        total_responses = total_result[0]["total"] if total_result else 0

        stats_result = list(
            responses_collection.aggregate(
                [
                    {"$match": {"survey_id": survey_object_id}},
                    {"$unwind": "$answers"},
                    {
                        "$lookup": {
                            "from": "questions",
                            "localField": "answers.question_id",
                            "foreignField": "_id",
                            "as": "question",
                        }
                    },
                    {"$unwind": "$question"},
                    {"$match": {"question.survey_id": survey_object_id}},
                    {
                        "$facet": {
                            "choice": [
                                {
                                    "$match": {
                                        "question.question_type": {
                                            "$in": [
                                                "single_choice",
                                                "multiple_choice",
                                            ]
                                        }
                                    }
                                },
                                {"$unwind": "$answers.value"},
                                {
                                    "$group": {
                                        "_id": {
                                            "question_id": "$answers.question_id",
                                            "option": "$answers.value",
                                        },
                                        "count": {"$sum": 1},
                                    }
                                },
                            ],
                            "text": [
                                {
                                    "$match": {
                                        "question.question_type": "text"
                                    }
                                },
                                {
                                    "$group": {
                                        "_id": "$answers.question_id",
                                        "values": {"$push": "$answers.value"},
                                    }
                                },
                            ],
                            "number": [
                                {
                                    "$match": {
                                        "question.question_type": "number",
                                        "answers.value": {"$type": "number"},
                                    }
                                },
                                {
                                    "$group": {
                                        "_id": "$answers.question_id",
                                        "count": {"$sum": 1},
                                        "total": {"$sum": "$answers.value"},
                                        "average": {"$avg": "$answers.value"},
                                    }
                                },
                            ],
                        }
                    },
                ]
            )
        )
        grouped_stats = stats_result[0] if stats_result else {}

        choice_counts = {}
        for item in grouped_stats.get("choice", []):
            question_id = str(item["_id"]["question_id"])
            option_value = str(item["_id"]["option"])
            choice_counts.setdefault(question_id, {})[option_value] = item["count"]

        text_values = {
            str(item["_id"]): item.get("values", [])
            for item in grouped_stats.get("text", [])
        }
        number_values = {
            str(item["_id"]): {
                "count": item.get("count", 0),
                "total": item.get("total", 0),
                "average": item.get("average"),
            }
            for item in grouped_stats.get("number", [])
        }

        question_stats = []
        for question in question_list:
            question_id = str(question["_id"])
            question_type = question["question_type"]
            if question_type in {"single_choice", "multiple_choice"}:
                options = [
                    {
                        "value": option.get("value"),
                        "label": option.get("label"),
                        "count": choice_counts.get(question_id, {}).get(
                            str(option.get("value")), 0
                        ),
                    }
                    for option in question.get("config", {}).get("options", [])
                ]
                statistics = {"options": options}
            elif question_type == "text":
                statistics = {
                    "responses": text_values.get(question_id, [])
                }
            elif question_type == "number":
                statistics = number_values.get(
                    question_id,
                    {"count": 0, "total": 0, "average": None},
                )
            else:
                statistics = {}

            question_stats.append(
                {
                    "question_id": question_id,
                    "title": question["title"],
                    "question_type": question_type,
                    "statistics": statistics,
                }
            )

        return jsonify(
            {
                "survey_id": str(survey_object_id),
                "survey_title": survey["title"],
                "total_responses": total_responses,
                "questions": question_stats,
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during survey statistics")
        return error_response("数据库暂时不可用", 503)


@app.post("/api/surveys/<survey_id>/questions")
@token_required
def create_question(survey_id):
    try:
        survey_object_id = ObjectId(survey_id)
    except InvalidId:
        return error_response("survey_id 格式无效", 400)

    payload = request.get_json(silent=True) or {}
    order = payload.get("order")
    title = payload.get("title")
    question_type = payload.get("question_type")
    is_required = payload.get("is_required")
    config = payload.get("config")
    routing_rules = payload.get("routing_rules")

    valid_question_types = {
        "single_choice",
        "multiple_choice",
        "text",
        "number",
    }
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        return error_response("order 必须是大于 0 的整数", 400)
    if not isinstance(title, str) or not title.strip():
        return error_response("title 必须是非空字符串", 400)
    if question_type not in valid_question_types:
        return error_response("question_type 不受支持", 400)
    if not isinstance(is_required, bool):
        return error_response("is_required 必须是布尔值", 400)
    if not isinstance(config, dict):
        return error_response("config 必须是嵌套文档", 400)
    if not isinstance(routing_rules, list):
        return error_response("routing_rules 必须是数组", 400)

    try:
        mongo_client.admin.command("ping")
        survey = surveys_collection.find_one(
            {
                "_id": survey_object_id,
                "creator_id": request.current_user_id,
            }
        )
        if survey is None:
            return error_response("问卷不存在或无权修改", 404)
        if survey.get("status") == "published":
            return error_response("已发布不可修改", 400)
        if survey.get("status") == "closed":
            return error_response("已关闭不可修改", 400)

        questions_collection.create_index(
            [("survey_id", ASCENDING), ("order", ASCENDING)],
            unique=True,
            name="uq_questions_survey_order",
        )
        question = {
            "survey_id": survey_object_id,
            "order": order,
            "title": title.strip(),
            "question_type": question_type,
            "is_required": is_required,
            "config": config,
            "routing_rules": routing_rules,
            "created_at": datetime.now(timezone.utc),
        }
        result = questions_collection.insert_one(question)
        question_id = result.inserted_id
        normalized_rules = []
        for rule in routing_rules:
            normalized_rule = dict(rule)
            normalized_conditions = []
            for condition in rule.get("conditions", []):
                normalized_condition = dict(condition)
                if normalized_condition.get("question_id") == "__CURRENT_QUESTION__":
                    normalized_condition["question_id"] = question_id
                normalized_conditions.append(normalized_condition)
            normalized_rule["conditions"] = normalized_conditions
            normalized_rules.append(normalized_rule)
        questions_collection.update_one(
            {"_id": question_id},
            {"$set": {"routing_rules": normalized_rules}},
        )
        return jsonify(
            {
                "message": "题目创建成功",
                "question_id": str(question_id),
            }
        ), 201
    except DuplicateKeyError:
        return error_response("该问卷已存在相同题号", 409)
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question creation")
        return error_response("数据库暂时不可用", 503)


@app.put("/api/surveys/<survey_id>/questions/<question_id>")
@token_required
def update_question(survey_id, question_id):
    try:
        survey_object_id = ObjectId(survey_id)
        question_object_id = ObjectId(question_id)
    except InvalidId:
        return error_response("survey_id 或 question_id 格式无效", 400)

    payload = request.get_json(silent=True) or {}
    order = payload.get("order")
    title = payload.get("title")
    question_type = payload.get("question_type")
    is_required = payload.get("is_required")
    config = payload.get("config")
    routing_rules = payload.get("routing_rules")
    valid_question_types = {
        "single_choice",
        "multiple_choice",
        "text",
        "number",
    }
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        return error_response("order 必须是大于 0 的整数", 400)
    if not isinstance(title, str) or not title.strip():
        return error_response("title 必须是非空字符串", 400)
    if question_type not in valid_question_types:
        return error_response("question_type 不受支持", 400)
    if not isinstance(is_required, bool):
        return error_response("is_required 必须是布尔值", 400)
    if not isinstance(config, dict):
        return error_response("config 必须是嵌套文档", 400)
    if not isinstance(routing_rules, list):
        return error_response("routing_rules 必须是数组", 400)

    normalized_rules = []
    for rule in routing_rules:
        normalized_rule = dict(rule)
        normalized_conditions = []
        for condition in rule.get("conditions", []):
            normalized_condition = dict(condition)
            if normalized_condition.get("question_id") == "__CURRENT_QUESTION__":
                normalized_condition["question_id"] = question_object_id
            normalized_conditions.append(normalized_condition)
        normalized_rule["conditions"] = normalized_conditions
        normalized_rules.append(normalized_rule)

    try:
        mongo_client.admin.command("ping")
        survey = surveys_collection.find_one(
            {
                "_id": survey_object_id,
                "creator_id": request.current_user_id,
            }
        )
        if survey is None:
            return error_response("问卷不存在或无权修改", 404)
        if survey.get("status") != "draft":
            return error_response("只有草稿问卷可以修改题目", 400)

        result = questions_collection.update_one(
            {
                "_id": question_object_id,
                "survey_id": survey_object_id,
            },
            {
                "$set": {
                    "order": order,
                    "title": title.strip(),
                    "question_type": question_type,
                    "is_required": is_required,
                    "config": config,
                    "routing_rules": normalized_rules,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        if result.matched_count == 0:
            return error_response("题目不存在或不属于该问卷", 404)
        return jsonify(
            {
                "message": "题目修改成功",
                "question_id": str(question_object_id),
            }
        )
    except DuplicateKeyError:
        return error_response("该问卷已存在相同题号", 409)
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question update")
        return error_response("数据库暂时不可用", 503)


@app.get("/api/surveys/<survey_id>/questions")
@token_required
def list_questions(survey_id):
    try:
        survey_object_id = ObjectId(survey_id)
    except InvalidId:
        return error_response("survey_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        survey = surveys_collection.find_one(
            {
                "_id": survey_object_id,
                "creator_id": request.current_user_id,
            }
        )
        if survey is None:
            return error_response("问卷不存在或无权查看", 404)

        questions = questions_collection.find(
            {"survey_id": survey_object_id}
        ).sort("order", ASCENDING)
        return jsonify(
            {
                "survey_id": str(survey_object_id),
                "questions": [serialize_question(question) for question in questions],
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question listing")
        return error_response("数据库暂时不可用", 503)


def _survey_is_available(survey):
    if survey.get("status") != "published":
        return False, "问卷当前未发布"
    deadline = survey.get("deadline")
    if deadline is not None:
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= datetime.now(timezone.utc):
            return False, "问卷已超过截止时间"
    return True, None


@app.get("/api/surveys/<survey_id>/fill")
def fill_survey(survey_id):
    try:
        survey_object_id = ObjectId(survey_id)
    except InvalidId:
        return error_response("survey_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        survey = surveys_collection.find_one({"_id": survey_object_id})
        if survey is None:
            return error_response("问卷不存在", 404)

        if survey.get("status") == "published":
            available, message = _survey_is_available(survey)
            if not available:
                return error_response(message, 403)
        elif survey.get("status") == "draft":
            try:
                preview_user_id = decode_bearer_token(
                    request.headers.get("Authorization", "")
                )
            except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, KeyError, InvalidId, ValueError):
                return error_response("草稿预览需要创建者 Token", 403)
            if preview_user_id != survey.get("creator_id"):
                return error_response("只有问卷创建者可以预览草稿", 403)
        else:
            return error_response("问卷当前不可填写", 403)

        questions = questions_collection.find(
            {"survey_id": survey_object_id}
        ).sort("order", ASCENDING)
        return jsonify(
            {
                "survey": serialize_survey(survey),
                "questions": [serialize_question(question) for question in questions],
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during survey filling")
        return error_response("数据库暂时不可用", 503)


@app.post("/api/surveys/<survey_id>/submit")
def submit_survey(survey_id):
    try:
        survey_object_id = ObjectId(survey_id)
    except InvalidId:
        return error_response("survey_id 格式无效", 400)

    user_id = None
    authorization = request.headers.get("Authorization")
    if authorization:
        try:
            user_id = decode_bearer_token(authorization)
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, KeyError, InvalidId, ValueError):
            return error_response("Token 无效或已过期", 401)

    payload = request.get_json(silent=True) or {}
    answers = payload.get("answers")
    visited_question_ids = payload.get("visited_question_ids")
    if not isinstance(answers, list) or not isinstance(visited_question_ids, list):
        return error_response("answers 和 visited_question_ids 必须是数组", 400)
    if not all(isinstance(question_id, str) for question_id in visited_question_ids):
        return error_response("visited_question_ids 必须包含字符串 ID", 400)
    if len(visited_question_ids) != len(set(visited_question_ids)):
        return error_response("visited_question_ids 不能包含重复题目", 400)

    try:
        visited_object_ids = [ObjectId(question_id) for question_id in visited_question_ids]
    except InvalidId:
        return error_response("visited_question_ids 中存在无效 ObjectId", 400)

    try:
        mongo_client.admin.command("ping")
        survey = surveys_collection.find_one({"_id": survey_object_id})
        if survey is None:
            return error_response("问卷不存在", 404)
        available, message = _survey_is_available(survey)
        if not available:
            return error_response(message, 403)
        if user_id is None and not survey.get("allow_anonymous", False):
            return error_response("该问卷不允许匿名提交", 401)

        questions = list(
            questions_collection.find({"survey_id": survey_object_id}).sort(
                "order", ASCENDING
            )
        )
        question_ids = {str(question["_id"]) for question in questions}
        unknown_ids = [question_id for question_id in visited_question_ids if question_id not in question_ids]
        if unknown_ids:
            return error_response("访问路径包含不属于该问卷的题目", 400)

        errors, answers_by_id = validate_answers(
            questions,
            answers,
            visited_question_ids,
        )
        try:
            routing_error = detect_routing_cycle(questions, answers_by_id)
        except (AttributeError, TypeError, ValueError):
            routing_error = "跳转规则格式无效"
        if routing_error:
            errors.append(routing_error)
        if errors:
            return jsonify({"error": "答卷校验失败", "details": errors}), 400

        answer_documents = []
        for answer in answers:
            answer_documents.append(
                {
                    "question_id": ObjectId(answer["question_id"]),
                    "value": answer.get("value"),
                }
            )
        response = {
            "survey_id": survey_object_id,
            "user_id": user_id,
            "is_anonymous": user_id is None,
            "answers": answer_documents,
            "visited_question_ids": visited_object_ids,
            "submitted_at": datetime.now(timezone.utc),
        }
        result = database["responses"].insert_one(response)
        return jsonify(
            {
                "message": "答卷提交成功",
                "response_id": str(result.inserted_id),
            }
        ), 201
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during survey submission")
        return error_response("数据库暂时不可用", 503)


@app.errorhandler(404)
def not_found(_error):
    return error_response("接口不存在", 404)


@app.errorhandler(500)
def internal_error(_error):
    return error_response("服务器内部错误", 500)


if __name__ == "__main__":
    app.run(debug=True)
