from pydantic import BaseModel, Field, HttpUrl
from typing import List, Dict, Optional, Any
from enum import Enum


class DocumentType(str, Enum):
    """Document types supported"""
    PASSPORT = "passport"
    ID_CARD = "id_card"
    DRIVERS_LICENSE = "drivers_license"
    FORM = "form"
    CONTRACT = "contract"
    APPLICATION = "application"
    CERTIFICATE = "certificate"
    OTHER = "other"


class FieldPosition(BaseModel):
    """Position of a field in the document"""
    x: float
    y: float
    width: float
    height: float
    page: int


class ExtractedField(BaseModel):
    """A field extracted from a document"""
    fieldName: str
    value: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    position: FieldPosition
    fieldType: Optional[str] = None
    rawText: Optional[str] = None


class DocumentMetadata(BaseModel):
    """Metadata about the processed document"""
    pageCount: int
    dimensions: Dict[str, float]
    fileSize: Optional[int] = None
    language: Optional[str] = None
    processingTime: Optional[float] = None


class OCRProcessRequest(BaseModel):
    """Request to process a document with OCR"""
    documentUrl: HttpUrl
    documentType: DocumentType
    userId: str
    options: Optional[Dict[str, Any]] = None


class OCRProcessResponse(BaseModel):
    """Response from OCR processing"""
    extractedFields: List[ExtractedField]
    documentMetadata: DocumentMetadata
    overallConfidence: float = Field(..., ge=0.0, le=1.0)
    processingStatus: str = "completed"


class FieldPlacement(BaseModel):
    """A field placement for auto-fill"""
    fieldName: str
    value: str
    x: float
    y: float
    width: float
    height: float
    page: int
    confidence: float = Field(..., ge=0.0, le=1.0)


class AutoFillRequest(BaseModel):
    """Request to analyze document for auto-fill"""
    documentUrl: HttpUrl
    documentType: DocumentType
    userData: Dict[str, Any]


class AutoFillResponse(BaseModel):
    """Response with field placements for auto-fill"""
    fieldPlacements: List[FieldPlacement]
    matchedFields: int
    unmatchedFields: List[str] = []


class ClassifyDocumentRequest(BaseModel):
    """Request to classify a document"""
    documentUrl: HttpUrl


class ClassifyDocumentResponse(BaseModel):
    """Response from document classification"""
    documentType: DocumentType
    confidence: float = Field(..., ge=0.0, le=1.0)
    possibleTypes: List[Dict[str, float]] = []


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    version: str
    ocrEngine: str
    services: Dict[str, str]
