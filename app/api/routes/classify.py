from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.models.schemas import ClassifyDocumentRequest, ClassifyDocumentResponse
from app.core.security import verify_api_key
from app.services.classification_service import document_classifier

router = APIRouter()


@router.post("/document", response_model=ClassifyDocumentResponse)
async def classify_document(
    request: ClassifyDocumentRequest,
    api_key: str = Depends(verify_api_key)
):
    """
    Classify document type
    
    Automatically detects the type of document (passport, ID card, form, etc.)
    using content analysis and layout patterns.
    
    - **documentUrl**: URL to the document
    
    Returns the detected document type with confidence score.
    """
    
    try:
        logger.info(f"Document classification requested")
        
        result = await document_classifier.classify_document(
            str(request.documentUrl)
        )
        
        return result
        
    except Exception as e:
        logger.error(f"Document classification failed: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Classification failed: {str(e)}"
        )
