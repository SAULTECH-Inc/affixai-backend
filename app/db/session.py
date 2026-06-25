"""FastAPI integration helpers for Tortoise ORM.

Tortoise manages its own connection pool — no per-request session dependency is
needed. Use `register_db(app)` in main.py during startup.
"""
from fastapi import FastAPI
from tortoise.contrib.fastapi import register_tortoise

from app.db.base import TORTOISE_ORM


def register_db(app: FastAPI, generate_schemas: bool = False) -> None:
    """Wire Tortoise into the FastAPI lifespan.

    `generate_schemas=True` is convenient in development; in production rely on
    aerich migrations instead.
    """
    register_tortoise(
        app,
        config=TORTOISE_ORM,
        generate_schemas=generate_schemas,
        add_exception_handlers=True,
    )
