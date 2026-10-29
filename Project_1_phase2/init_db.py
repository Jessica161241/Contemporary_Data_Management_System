import os

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError


MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb://localhost:27017/survey_db",
)


USER_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["username", "password_hash", "created_at"],
        "properties": {
            "username": {
                "bsonType": "string",
                "minLength": 3,
                "maxLength": 50,
                "description": "用户名必须是 3 至 50 个字符",
            },
            "password_hash": {
                "bsonType": "string",
                "minLength": 20,
                "description": "这里只允许保存密码哈希",
            },
            "created_at": {
                "bsonType": "date",
            },
        },
    }
}


SURVEY_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "title",
            "description",
            "allow_anonymous",
            "status",
            "creator_id",
            "created_at",
            "updated_at",
        ],
        "properties": {
            "title": {
                "bsonType": "string",
                "minLength": 1,
                "maxLength": 200,
            },
            "description": {
                "bsonType": "string",
                "maxLength": 5000,
            },
            "allow_anonymous": {
                "bsonType": "bool",
            },
            "status": {
                "enum": ["draft", "published", "closed"],
            },
            "deadline": {
                "bsonType": ["date", "null"],
            },
            "creator_id": {
                "bsonType": "objectId",
            },
            "created_at": {
                "bsonType": "date",
            },
            "updated_at": {
                "bsonType": "date",
            },
        },
    }
}


def recreate_collection(database, name, validator=None):
    """Drop and recreate a collection so this script can safely reset the schema."""
    database[name].drop()
    options = {}
    if validator is not None:
        options.update(
            {
                "validator": validator,
                "validationLevel": "strict",
                "validationAction": "error",
            }
        )
    return database.create_collection(name, **options)


def initialize_database():
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
        database = client.get_default_database()

        users = recreate_collection(database, "users", USER_VALIDATOR)
        surveys = recreate_collection(database, "surveys", SURVEY_VALIDATOR)
        questions = recreate_collection(database, "questions")
        responses = recreate_collection(database, "responses")

        users.create_index(
            [("username", ASCENDING)],
            unique=True,
            name="uq_users_username",
        )

        surveys.create_index(
            [("creator_id", ASCENDING), ("created_at", DESCENDING)],
            name="idx_surveys_creator_created",
        )
        surveys.create_index(
            [("status", ASCENDING), ("created_at", DESCENDING)],
            name="idx_surveys_status_created",
        )
        surveys.create_index(
            [("deadline", ASCENDING)],
            name="idx_surveys_deadline",
        )

        questions.create_index(
            [("survey_id", ASCENDING), ("order", ASCENDING)],
            unique=True,
            name="uq_questions_survey_order",
        )

        responses.create_index(
            [("survey_id", ASCENDING)],
            name="idx_responses_survey",
        )
        responses.create_index(
            [("survey_id", ASCENDING), ("submitted_at", DESCENDING)],
            name="idx_responses_survey_submitted",
        )
        responses.create_index(
            [("user_id", ASCENDING)],
            name="idx_responses_user",
            sparse=True,
        )

        print(f"MongoDB schema initialized: {database.name}")
    except PyMongoError as error:
        print(f"MongoDB initialization failed: {error}")
        raise
    finally:
        client.close()


if __name__ == "__main__":
    initialize_database()
