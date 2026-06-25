from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.models.schemas import OCRProcessRequest, OCRProcessResponse
from app.core.security import verify_api_key
from app.services.document_processor import document_processor

router = APIRouter()


@router.post("/process", response_model=OCRProcessResponse)
async def process_document(
    request: OCRProcessRequest,
    api_key: str = Depends(verify_api_key)
):
    """
    Process a document with OCR
    
    Extracts text and field positions from a document using OCR technology.
    Supports PDFs and images (PNG, JPG, TIFF).
    
    - **documentUrl**: URL to the document (can be S3 presigned URL or HTTP)
    - **documentType**: Type of document (passport, form, etc.)
    - **userId**: User ID for tracking
    """
    
    try:
        logger.info(
            f"OCR request for user {request.userId}: "
            f"{request.documentType}"
        )
        
        result = await document_processor.process_document(
            str(request.documentUrl),
            request.documentType.value
        )
        
        return result
        
    except Exception as e:
        logger.error(f"OCR processing failed: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"OCR processing failed: {str(e)}"
        )
