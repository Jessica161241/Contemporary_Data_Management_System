import os


class Config:
    MONGO_URI = os.getenv(
        "MONGO_URI",
        "mongodb://localhost:27017/survey_db",
    )
    JWT_SECRET_KEY = os.getenv(
        "JWT_SECRET_KEY",
        "change-this-secret-in-production",
    )
    JWT_EXPIRES_HOURS = 24
