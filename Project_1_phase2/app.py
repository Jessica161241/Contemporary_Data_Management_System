from datetime import datetime, timedelta, timezone
from functools import wraps
import math
from copy import deepcopy

import jwt
from bson import ObjectId
from bson.errors import InvalidId
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from pymongo import ASCENDING, DESCENDING, MongoClient
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
question_history_collection = database["question_history"]
responses_collection = database["responses"]
question_bank_collection = database["question_bank"]
question_bank_shares_collection = database["question_bank_shares"]


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
        **(
            {
                "bank_root_id": str(question["bank_root_id"]),
                "bank_version": question.get("bank_version"),
                "source_bank_version_id": str(question["source_bank_version_id"])
                if question.get("source_bank_version_id") is not None
                else None,
            }
            if question.get("bank_root_id") is not None
            else {}
        ),
    }


def serialize_question_bank(question, is_shared=False, shared_owner=None):
    """Convert a question-bank version into a JSON-compatible document."""
    return {
        "id": str(question["_id"]),
        "root_id": str(question["root_id"]),
        "version": question["version"],
        "is_latest": question["is_latest"],
        "owner_id": str(question["owner_id"]),
        "title": question["title"],
        "question_type": question["question_type"],
        "is_required": question["is_required"],
        "config": question.get("config", {}),
        "routing_rules": question.get("routing_rules", []),
        "visibility": question.get("visibility", "private"),
        "status": question.get("status", "active"),
        "is_shared": is_shared,
        "shared_owner": shared_owner,
        "created_at": question["created_at"].isoformat(),
        "updated_at": question["updated_at"].isoformat(),
    }


def validate_question_bank_payload(payload):
    """Validate the common fields accepted by question-bank endpoints."""
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

    if not isinstance(title, str) or not title.strip():
        return None, "title 必须是非空字符串"
    if question_type not in valid_question_types:
        return None, "question_type 不受支持"
    if not isinstance(is_required, bool):
        return None, "is_required 必须是布尔值"
    if not isinstance(config, dict):
        return None, "config 必须是嵌套文档"
    if not isinstance(routing_rules, list):
        return None, "routing_rules 必须是数组"

    return {
        "title": title.strip(),
        "question_type": question_type,
        "is_required": is_required,
        "config": config,
        "routing_rules": routing_rules,
    }, None


def get_question_bank_accessible_version(root_id, user_id):
    """Return the latest bank version when the user may access the root."""
    latest = question_bank_collection.find_one(
        {"root_id": root_id, "is_latest": True}
    )
    if latest is None:
        return None
    if latest.get("owner_id") == user_id or latest.get("visibility") == "public":
        return latest
    shared = question_bank_shares_collection.find_one(
        {"root_id": root_id, "user_id": user_id}
    )
    return latest if shared is not None else None


def save_question_to_bank_v1(question_data, owner_id):
    now = datetime.now(timezone.utc)
    root_id = ObjectId()
    question = {
        "_id": root_id,
        "root_id": root_id,
        "version": 1,
        "is_latest": True,
        "owner_id": owner_id,
        **deepcopy(question_data),
        "visibility": "private",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    question_bank_collection.insert_one(question)
    return question


@app.post("/api/question_bank")
@token_required
def create_question_bank_question():
    payload = request.get_json(silent=True) or {}
    question_data, validation_error = validate_question_bank_payload(payload)
    if validation_error:
        return error_response(validation_error, 400)

    now = datetime.now(timezone.utc)
    root_id = ObjectId()
    question = {
        "_id": root_id,
        "root_id": root_id,
        "version": 1,
        "is_latest": True,
        "owner_id": request.current_user_id,
        **question_data,
        "visibility": "private",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }

    try:
        mongo_client.admin.command("ping")
        question_bank_collection.insert_one(question)
        return jsonify(
            {
                "message": "题目已保存到题库",
                "question": serialize_question_bank(question),
            }
        ), 201
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question-bank creation")
        return error_response("数据库暂时不可用", 503)


@app.get("/api/question_bank")
@token_required
def list_question_bank_questions():
    try:
        mongo_client.admin.command("ping")
        own_questions = list(question_bank_collection.find(
            {
                "owner_id": request.current_user_id,
                "is_latest": True,
            }
        ).sort("updated_at", -1))
        shares = list(question_bank_shares_collection.find(
            {"user_id": request.current_user_id},
            {"root_id": 1},
        ))
        shared_root_ids = {share["root_id"] for share in shares}
        shared_questions = list(question_bank_collection.find(
            {
                "root_id": {"$in": list(shared_root_ids)},
                "is_latest": True,
                "owner_id": {"$ne": request.current_user_id},
            }
        ).sort("updated_at", -1)) if shared_root_ids else []
        owner_ids = {question["owner_id"] for question in shared_questions}
        owners = users_collection.find({"_id": {"$in": list(owner_ids)}})
        owner_names = {owner["_id"]: owner["username"] for owner in owners}
        serialized_questions = [
            serialize_question_bank(question, is_shared=False)
            for question in own_questions
        ]
        serialized_questions.extend(
            serialize_question_bank(
                question,
                is_shared=True,
                shared_owner=owner_names.get(question["owner_id"]),
            )
            for question in shared_questions
        )
        serialized_questions.sort(
            key=lambda question: question["updated_at"],
            reverse=True,
        )
        return jsonify(
            {
                "questions": serialized_questions
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question-bank listing")
        return error_response("数据库暂时不可用", 503)


@app.put("/api/question_bank/<root_id>")
@token_required
def update_question_bank_question(root_id):
    try:
        root_object_id = ObjectId(root_id)
    except InvalidId:
        return error_response("root_id 格式无效", 400)

    payload = request.get_json(silent=True) or {}
    question_data, validation_error = validate_question_bank_payload(payload)
    if validation_error:
        return error_response(validation_error, 400)

    try:
        mongo_client.admin.command("ping")
        latest = question_bank_collection.find_one(
            {
                "root_id": root_object_id,
                "owner_id": request.current_user_id,
                "is_latest": True,
            }
        )
        if latest is None:
            return error_response("题目不存在或无权修改", 404)

        question_bank_collection.update_one(
            {"_id": latest["_id"]},
            {"$set": {"is_latest": False, "updated_at": datetime.now(timezone.utc)}},
        )
        now = datetime.now(timezone.utc)
        new_question = {
            "_id": ObjectId(),
            "root_id": root_object_id,
            "version": latest["version"] + 1,
            "is_latest": True,
            "owner_id": latest["owner_id"],
            **question_data,
            "visibility": latest.get("visibility", "private"),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        question_bank_collection.insert_one(new_question)
        return jsonify(
            {
                "message": "题库题目已生成新版本",
                "question": serialize_question_bank(new_question),
            }
        )
    except DuplicateKeyError:
        return error_response("题目版本号冲突，请重试", 409)
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question-bank update")
        return error_response("数据库暂时不可用", 503)


@app.delete("/api/question_bank/<root_id>")
@token_required
def delete_question_bank_question(root_id):
    try:
        root_object_id = ObjectId(root_id)
    except InvalidId:
        return error_response("root_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        latest = question_bank_collection.find_one(
            {
                "root_id": root_object_id,
                "owner_id": request.current_user_id,
                "is_latest": True,
            }
        )
        if latest is None:
            return error_response("题目不存在或无权删除", 404)

        if questions_collection.find_one({"bank_root_id": root_object_id}):
            return error_response("该题已被问卷使用，无法删除", 400)

        question_bank_collection.delete_many({"root_id": root_object_id})
        question_bank_shares_collection.delete_many({"root_id": root_object_id})
        return jsonify({"message": "题库题目已删除", "root_id": root_id})
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question-bank deletion")
        return error_response("数据库暂时不可用", 503)


@app.post("/api/question_bank/<root_id>/revert/<int:version>")
@token_required
def revert_question_bank_version(root_id, version):
    try:
        root_object_id = ObjectId(root_id)
    except InvalidId:
        return error_response("root_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        root_question = question_bank_collection.find_one(
            {
                "root_id": root_object_id,
                "owner_id": request.current_user_id,
            },
            {"_id": 1},
        )
        if root_question is None:
            return error_response("题目不存在或无权恢复版本", 404)

        version_number = int(version)
        if question_bank_collection.find_one(
            {"root_id": root_object_id, "version": version_number},
            {"_id": 1},
        ) is None:
            return error_response("指定的历史版本不存在", 404)

        question_bank_collection.update_many(
            {"root_id": root_object_id},
            {"$set": {"is_latest": False}},
        )
        result = question_bank_collection.update_one(
            {"root_id": root_object_id, "version": version_number},
            {"$set": {"is_latest": True}},
        )
        if result.matched_count == 0:
            return error_response("指定的历史版本不存在", 404)
        return jsonify(
            {
                "message": "已切换题库最新版本",
                "root_id": str(root_object_id),
                "version": version_number,
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question-bank version revert")
        return error_response("数据库暂时不可用", 503)


@app.get("/api/question_bank/<root_id>/history")
@token_required
def question_bank_history(root_id):
    try:
        root_object_id = ObjectId(root_id)
    except InvalidId:
        return error_response("root_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        latest = get_question_bank_accessible_version(
            root_object_id,
            request.current_user_id,
        )
        if latest is None:
            return error_response("题目不存在或无权查看历史版本", 404)

        history = question_bank_collection.find(
            {"root_id": root_object_id}
        ).sort("version", -1)
        return jsonify(
            {
                "root_id": str(root_object_id),
                "versions": [
                    serialize_question_bank(question) for question in history
                ],
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question-bank history")
        return error_response("数据库暂时不可用", 503)


@app.get("/api/question_bank/<root_id>/usage")
@token_required
def question_bank_usage(root_id):
    try:
        root_object_id = ObjectId(root_id)
    except InvalidId:
        return error_response("root_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        if get_question_bank_accessible_version(
            root_object_id,
            request.current_user_id,
        ) is None:
            return error_response("题目不存在或无权查看引用", 404)

        survey_ids = {
            question["survey_id"]
            for question in questions_collection.find(
                {"bank_root_id": root_object_id},
                {"survey_id": 1},
            )
        }
        surveys = surveys_collection.find(
            {"_id": {"$in": list(survey_ids)}},
            {"title": 1},
        ).sort("created_at", DESCENDING)
        return jsonify(
            {
                "root_id": str(root_object_id),
                "surveys": [
                    {"survey_id": str(survey["_id"]), "title": survey["title"]}
                    for survey in surveys
                ],
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question-bank usage lookup")
        return error_response("数据库暂时不可用", 503)


@app.post("/api/question_bank/<root_id>/share")
@token_required
def share_question_bank_question(root_id):
    try:
        root_object_id = ObjectId(root_id)
    except InvalidId:
        return error_response("root_id 格式无效", 400)

    payload = request.get_json(silent=True) or {}
    target_user_id = payload.get("user_id")
    username = payload.get("username")
    if not target_user_id and not isinstance(username, str):
        return error_response("请提供目标用户的 username 或 user_id", 400)

    try:
        mongo_client.admin.command("ping")
        latest = question_bank_collection.find_one(
            {
                "root_id": root_object_id,
                "owner_id": request.current_user_id,
                "is_latest": True,
            }
        )
        if latest is None:
            return error_response("题目不存在或无权共享", 404)

        if target_user_id:
            try:
                target_user_object_id = ObjectId(target_user_id)
            except InvalidId:
                return error_response("user_id 格式无效", 400)
            target_user = users_collection.find_one({"_id": target_user_object_id})
        else:
            target_user = users_collection.find_one(
                {"username": username.strip().casefold()}
            )
            target_user_object_id = target_user["_id"] if target_user else None

        if target_user is None:
            return error_response("目标用户不存在", 404)
        if target_user_object_id == request.current_user_id:
            return error_response("不能共享给自己", 400)

        share = {
            "root_id": root_object_id,
            "user_id": target_user_object_id,
            "role": "reader",
            "created_by": request.current_user_id,
            "created_at": datetime.now(timezone.utc),
        }
        question_bank_shares_collection.insert_one(share)
        question_bank_collection.update_many(
            {"root_id": root_object_id},
            {"$set": {"visibility": "shared", "updated_at": datetime.now(timezone.utc)}},
        )
        return jsonify(
            {
                "message": "题目共享成功",
                "root_id": str(root_object_id),
                "user_id": str(target_user_object_id),
                "role": "reader",
            }
        ), 201
    except DuplicateKeyError:
        return error_response("该题目已经共享给目标用户", 409)
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question-bank sharing")
        return error_response("数据库暂时不可用", 503)


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


@app.get("/bank.html")
def bank_page():
    return render_template("bank.html")


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
    save_to_bank = payload.get("save_to_bank", False)

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
    if not isinstance(save_to_bank, bool):
        return error_response("save_to_bank 必须是布尔值", 400)

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
        question_id = ObjectId()
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

        question = {
            "_id": question_id,
            "survey_id": survey_object_id,
            "order": order,
            "title": title.strip(),
            "question_type": question_type,
            "is_required": is_required,
            "config": config,
            "routing_rules": normalized_rules,
            "created_at": datetime.now(timezone.utc),
        }
        questions_collection.insert_one(question)
        if save_to_bank:
            bank_question = None
            try:
                bank_question = save_question_to_bank_v1(
                    {
                        "title": title.strip(),
                        "question_type": question_type,
                        "is_required": is_required,
                        "config": config,
                        "routing_rules": normalized_rules,
                    },
                    request.current_user_id,
                )
                questions_collection.update_one(
                    {"_id": question_id, "survey_id": survey_object_id},
                    {
                        "$set": {
                            "bank_root_id": bank_question["root_id"],
                            "bank_version": bank_question["version"],
                            "source_bank_version_id": bank_question["_id"],
                            "snapshot_at": datetime.now(timezone.utc),
                        }
                    },
                )
                question.update(
                    {
                        "bank_root_id": bank_question["root_id"],
                        "bank_version": bank_question["version"],
                        "source_bank_version_id": bank_question["_id"],
                    }
                )
            except PyMongoError:
                if bank_question is not None:
                    question_bank_collection.delete_one({"_id": bank_question["_id"]})
                questions_collection.delete_one({"_id": question_id})
                raise
        return jsonify(
            {
                "message": "题目创建成功",
                "question_id": str(question_id),
                "question": serialize_question(question),
            }
        ), 201
    except DuplicateKeyError:
        return error_response("该问卷已存在相同题号", 409)
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question creation")
        return error_response("数据库暂时不可用", 503)


@app.post("/api/surveys/<survey_id>/questions/from_bank")
@token_required
def add_question_from_bank(survey_id):
    try:
        survey_object_id = ObjectId(survey_id)
    except InvalidId:
        return error_response("survey_id 格式无效", 400)

    payload = request.get_json(silent=True) or {}
    order = payload.get("order")
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        return error_response("order 必须是大于 0 的整数", 400)

    bank_version_id = payload.get("bank_version_id")
    root_id = payload.get("root_id")
    version = payload.get("version")
    try:
        if bank_version_id:
            bank_version_object_id = ObjectId(bank_version_id)
            bank_question = question_bank_collection.find_one(
                {"_id": bank_version_object_id}
            )
        else:
            if not root_id or not isinstance(version, int) or version < 1:
                return error_response(
                    "请提供有效的 bank_version_id，或 root_id 与 version",
                    400,
                )
            root_object_id = ObjectId(root_id)
            bank_question = question_bank_collection.find_one(
                {"root_id": root_object_id, "version": version}
            )
    except InvalidId:
        return error_response("题库版本 ID 或 root_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        if bank_question is None:
            return error_response("题库版本不存在", 404)

        accessible = get_question_bank_accessible_version(
            bank_question["root_id"],
            request.current_user_id,
        )
        if accessible is None:
            return error_response("无权使用该题库题目", 403)

        survey = surveys_collection.find_one(
            {"_id": survey_object_id, "creator_id": request.current_user_id}
        )
        if survey is None:
            return error_response("问卷不存在或无权修改", 404)
        if survey.get("status") != "draft":
            return error_response("只有草稿问卷可以添加题目", 400)

        snapshot_id = ObjectId()
        snapshot_rules = deepcopy(bank_question.get("routing_rules", []))
        for rule in snapshot_rules:
            for condition in rule.get("conditions", []):
                if condition.get("question_id") == "__CURRENT_QUESTION__":
                    condition["question_id"] = snapshot_id

        now = datetime.now(timezone.utc)
        snapshot = {
            "_id": snapshot_id,
            "survey_id": survey_object_id,
            "order": order,
            "title": bank_question["title"],
            "question_type": bank_question["question_type"],
            "is_required": bank_question["is_required"],
            "config": deepcopy(bank_question.get("config", {})),
            "routing_rules": snapshot_rules,
            "bank_root_id": bank_question["root_id"],
            "bank_version": bank_question["version"],
            "source_bank_version_id": bank_question["_id"],
            "snapshot_at": now,
            "created_at": now,
            "updated_at": now,
        }
        questions_collection.insert_one(snapshot)
        return jsonify(
            {
                "message": "题库题目已作为快照加入问卷",
                "question_id": str(snapshot_id),
                "bank_root_id": str(bank_question["root_id"]),
                "bank_version": bank_question["version"],
            }
        ), 201
    except DuplicateKeyError:
        return error_response("该问卷已存在相同题号", 409)
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during bank snapshot creation")
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
    save_to_bank = payload.get("save_to_bank", False)
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
    if not isinstance(save_to_bank, bool):
        return error_response("save_to_bank 必须是布尔值", 400)

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

        old_question = questions_collection.find_one(
            {"_id": question_object_id, "survey_id": survey_object_id}
        )
        if old_question is None:
            return error_response("题目不存在或不属于该问卷", 404)
        latest_history = question_history_collection.find_one(
            {
                "survey_id": survey_object_id,
                "question_id": question_object_id,
            },
            sort=[("version", -1)],
        )
        history_version = (latest_history["version"] + 1) if latest_history else 1
        question_history_collection.insert_one(
            {
                "survey_id": survey_object_id,
                "question_id": question_object_id,
                "version": history_version,
                "updated_at": datetime.now(timezone.utc),
                "old_data": deepcopy(old_question),
            }
        )

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
        if save_to_bank:
            save_question_to_bank_v1(
                {
                    "title": title.strip(),
                    "question_type": question_type,
                    "is_required": is_required,
                    "config": config,
                    "routing_rules": normalized_rules,
                },
                request.current_user_id,
            )
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


@app.put("/api/surveys/<survey_id>/questions/<question_id>/bank-link")
@token_required
def link_question_to_bank(survey_id, question_id):
    try:
        survey_object_id = ObjectId(survey_id)
        question_object_id = ObjectId(question_id)
    except InvalidId:
        return error_response("survey_id 或 question_id 格式无效", 400)

    payload = request.get_json(silent=True) or {}
    bank_root_id = payload.get("bank_root_id")
    bank_version = payload.get("bank_version")
    try:
        bank_root_object_id = ObjectId(bank_root_id)
    except (InvalidId, TypeError):
        return error_response("bank_root_id 格式无效", 400)
    if not isinstance(bank_version, int) or isinstance(bank_version, bool) or bank_version < 1:
        return error_response("bank_version 必须是大于 0 的整数", 400)

    try:
        mongo_client.admin.command("ping")
        survey = surveys_collection.find_one(
            {"_id": survey_object_id, "creator_id": request.current_user_id}
        )
        if survey is None or survey.get("status") != "draft":
            return error_response("问卷不存在、无权修改或当前不可编辑", 404)

        bank_question = question_bank_collection.find_one(
            {
                "root_id": bank_root_object_id,
                "version": bank_version,
                "owner_id": request.current_user_id,
            }
        )
        if bank_question is None:
            return error_response("题库版本不存在或无权关联", 404)

        result = questions_collection.update_one(
            {"_id": question_object_id, "survey_id": survey_object_id},
            {
                "$set": {
                    "bank_root_id": bank_root_object_id,
                    "bank_version": bank_version,
                    "source_bank_version_id": bank_question["_id"],
                    "snapshot_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        if result.matched_count == 0:
            return error_response("问卷题目不存在", 404)

        return jsonify(
            {
                "message": "问卷题目已关联到题库",
                "question_id": str(question_object_id),
                "bank_root_id": str(bank_root_object_id),
                "bank_version": bank_version,
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed while linking question to bank")
        return error_response("数据库暂时不可用", 503)


@app.get("/api/surveys/<survey_id>/questions/<question_id>/history")
@token_required
def question_history(survey_id, question_id):
    try:
        survey_object_id = ObjectId(survey_id)
        question_object_id = ObjectId(question_id)
    except InvalidId:
        return error_response("survey_id 或 question_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        survey = surveys_collection.find_one(
            {"_id": survey_object_id, "creator_id": request.current_user_id}
        )
        if survey is None:
            return error_response("问卷不存在或无权查看历史", 404)
        records = question_history_collection.find(
            {"survey_id": survey_object_id, "question_id": question_object_id}
        ).sort("version", -1)
        history = []
        for record in records:
            old_data = record.get("old_data", {})
            history.append(
                {
                    "version": record["version"],
                    "updated_at": record["updated_at"].isoformat(),
                    "old_data": serialize_question(old_data),
                }
            )
        return jsonify(
            {
                "survey_id": str(survey_object_id),
                "question_id": str(question_object_id),
                "history": history,
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question history")
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


@app.get("/api/question_bank/<root_id>/stats")
@token_required
def question_bank_stats(root_id):
    try:
        root_object_id = ObjectId(root_id)
    except InvalidId:
        return error_response("root_id 格式无效", 400)

    try:
        mongo_client.admin.command("ping")
        if get_question_bank_accessible_version(
            root_object_id,
            request.current_user_id,
        ) is None:
            return error_response("无权查看该题目的跨问卷统计", 403)

        snapshots = list(
            questions_collection.find({"bank_root_id": root_object_id}).sort(
                "bank_version", ASCENDING
            )
        )
        snapshot_ids = [question["_id"] for question in snapshots]
        all_bank_questions = list(
            question_bank_collection.find({"root_id": root_object_id}).sort(
                "version", ASCENDING
            )
        )
        bank_versions = {question["version"] for question in all_bank_questions}
        bank_questions = {
            question["version"]: question for question in all_bank_questions
        }
        survey_ids = {question["survey_id"] for question in snapshots}
        version_survey_ids = {}
        for snapshot in snapshots:
            version_survey_ids.setdefault(snapshot.get("bank_version"), set()).add(
                snapshot["survey_id"]
            )
        survey_documents = list(
            surveys_collection.find(
                {"_id": {"$in": list(survey_ids)}},
                {"title": 1},
            )
        )
        survey_titles = {
            survey["_id"]: survey.get("title", "未命名问卷")
            for survey in survey_documents
        }
        base_pipeline = [
            {"$match": {"answers.question_id": {"$in": snapshot_ids}}},
            {"$unwind": "$answers"},
            {"$match": {"answers.question_id": {"$in": snapshot_ids}}},
            {
                "$lookup": {
                    "from": "questions",
                    "localField": "answers.question_id",
                    "foreignField": "_id",
                    "as": "question",
                }
            },
            {"$unwind": "$question"},
            {"$match": {"question.bank_root_id": root_object_id}},
            {"$set": {"bank_version_key": "$question.bank_version"}},
        ]
        stats_result = list(
            responses_collection.aggregate(
                base_pipeline
                + [
                    {
                        "$facet": {
                            "all_responses": [
                                {"$group": {"_id": "$_id"}},
                                {"$count": "total"},
                            ],
                            "response_count": [
                                {
                                    "$group": {
                                        "_id": {
                                            "bank_version": "$bank_version_key",
                                            "response_id": "$_id",
                                        }
                                    }
                                },
                                {
                                    "$group": {
                                        "_id": "$_id.bank_version",
                                        "count": {"$sum": 1},
                                    }
                                },
                            ],
                            "survey_response_count": [
                                {
                                    "$group": {
                                        "_id": {
                                            "bank_version": "$bank_version_key",
                                            "survey_id": "$question.survey_id",
                                            "response_id": "$_id",
                                        }
                                    }
                                },
                                {
                                    "$group": {
                                        "_id": {
                                            "bank_version": "$_id.bank_version",
                                            "survey_id": "$_id.survey_id",
                                        },
                                        "count": {"$sum": 1},
                                    }
                                },
                            ],
                            "survey_response_count_all": [
                                {
                                    "$group": {
                                        "_id": {
                                            "survey_id": "$question.survey_id",
                                            "response_id": "$_id",
                                        }
                                    }
                                },
                                {
                                    "$group": {
                                        "_id": "$_id.survey_id",
                                        "count": {"$sum": 1},
                                    }
                                },
                            ],
                            "choice": [
                                {
                                    "$match": {
                                        "question.question_type": {
                                            "$in": ["single_choice", "multiple_choice"]
                                        }
                                    }
                                },
                                {"$unwind": "$answers.value"},
                                {
                                    "$group": {
                                        "_id": {
                                            "bank_version": "$bank_version_key",
                                            "option": "$answers.value",
                                        },
                                        "count": {"$sum": 1},
                                    }
                                },
                            ],
                            "text": [
                                {"$match": {"question.question_type": "text"}},
                                {
                                    "$group": {
                                        "_id": "$bank_version_key",
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
                                        "_id": "$bank_version_key",
                                        "count": {"$sum": 1},
                                        "total": {"$sum": "$answers.value"},
                                        "average": {"$avg": "$answers.value"},
                                    }
                                },
                            ],
                        }
                    }
                ]
            )
        )
        grouped = stats_result[0] if stats_result else {}
        all_response_result = grouped.get("all_responses", [])
        total_responses = all_response_result[0]["total"] if all_response_result else 0
        version_response_counts = {
            item["_id"]: item.get("count", 0)
            for item in grouped.get("response_count", [])
        }
        version_survey_response_counts = {}
        for item in grouped.get("survey_response_count", []):
            version = item["_id"]["bank_version"]
            survey_id = item["_id"]["survey_id"]
            version_survey_response_counts.setdefault(version, {})[survey_id] = item.get(
                "count", 0
            )
        survey_response_counts = {
            item["_id"]: item.get("count", 0)
            for item in grouped.get("survey_response_count_all", [])
        }
        survey_breakdown = [
            {
                "survey_id": str(survey_id),
                "title": survey_titles.get(survey_id, "未命名问卷"),
                "response_count": survey_response_counts.get(survey_id, 0),
            }
            for survey_id in survey_ids
        ]
        survey_breakdown.sort(key=lambda survey: survey["title"])
        version_choice_counts = {}
        for item in grouped.get("choice", []):
            version = item["_id"]["bank_version"]
            option = str(item["_id"]["option"])
            version_choice_counts.setdefault(version, {})[option] = item["count"]
        version_text_values = {
            item["_id"]: item.get("values", [])
            for item in grouped.get("text", [])
        }
        version_number_values = {
            item["_id"]: {
                "count": item.get("count", 0),
                "total": item.get("total", 0),
                "average": item.get("average"),
            }
            for item in grouped.get("number", [])
        }

        question_stats = []
        for version in sorted(bank_versions):
            question = bank_questions.get(version)
            if question is None:
                continue
            version_survey_ids_for_version = version_survey_ids.get(version, set())
            version_survey_counts = version_survey_response_counts.get(version, {})
            question_type = question["question_type"]
            if question_type in {"single_choice", "multiple_choice"}:
                statistics = {
                    "options": [
                        {
                            "value": option.get("value"),
                            "label": option.get("label"),
                            "count": version_choice_counts.get(version, {}).get(
                                str(option.get("value")), 0
                            ),
                        }
                        for option in question.get("config", {}).get("options", [])
                    ]
                }
            elif question_type == "text":
                statistics = {"responses": version_text_values.get(version, [])}
            elif question_type == "number":
                statistics = version_number_values.get(
                    version, {"count": 0, "total": 0, "average": None}
                )
            else:
                statistics = {}
            question_stats.append(
                {
                    "question_id": str(question["_id"]),
                    "bank_version": version,
                    "title": question["title"],
                    "question_type": question_type,
                    "statistics": statistics,
                    "version_responses": version_response_counts.get(version, 0),
                    "total_response_count": version_response_counts.get(version, 0),
                    "total_survey_count": len(version_survey_ids_for_version),
                    "survey_breakdown": sorted(
                        [
                            {
                                "survey_id": str(survey_id),
                                "title": survey_titles.get(survey_id, "未命名问卷"),
                                "response_count": version_survey_counts.get(survey_id, 0),
                            }
                            for survey_id in version_survey_ids_for_version
                        ],
                        key=lambda survey: survey["title"],
                    ),
                }
            )

        return jsonify(
            {
                "root_id": str(root_object_id),
                "total_responses": total_responses,
                "total_survey_count": len(survey_ids),
                "total_response_count": total_responses,
                "survey_breakdown": survey_breakdown,
                "questions": question_stats,
            }
        )
    except PyMongoError:
        app.logger.exception("MongoDB operation failed during question-bank statistics")
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
