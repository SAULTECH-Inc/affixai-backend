"""Helpers to call our own AI service routes (OCR, autofill, classify) in-process.

The old NestJS code went over HTTP; here we just call the service objects directly,
since they live in the same Python process.
"""
from __future__ import annotations

from typing import Any

from app.services.autofill_service import autofill_service
from app.services.classification_service import document_classifier
from app.services.document_processor import document_processor


async def process_document(document_url: str, document_type: str) -> Any:
    return await document_processor.process_document(document_url, document_type)


async def auto_fill_document(
    document_url: str, document_type: str, user_data: dict[str, Any]
) -> Any:
    return await autofill_service.analyze_for_autofill(document_url, document_type, user_data)


async def classify_document(document_url: str) -> Any:
    return await document_classifier.classify_document(document_url)
