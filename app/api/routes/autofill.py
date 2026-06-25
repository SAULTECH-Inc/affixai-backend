from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.models.schemas import AutoFillRequest, AutoFillResponse
from app.core.security import verify_api_key
from app.services.autofill_service import autofill_service

router = APIRouter()


@router.post("/analyze", response_model=AutoFillResponse)
async def analyze_for_autofill(
    request: AutoFillRequest,
    api_key: str = Depends(verify_api_key)
):
    """
    Analyze document for auto-fill
    
    Analyzes document structure and matches extracted fields with user data
    to determine optimal field placements for automatic filling.
    
    - **documentUrl**: URL to the document
    - **documentType**: Type of document
    - **userData**: User's data to fill (key-value pairs)
    
    Returns field placements with coordinates and confidence scores.
    """
    
    try:
        logger.info(
            f"Auto-fill analysis: {request.documentType} "
            f"with {len(request.userData)} user fields"
        )
        
        result = await autofill_service.analyze_for_autofill(
            str(request.documentUrl),
            request.documentType.value,
            request.userData
        )
        
        return result
        
    except Exception as e:
        logger.error(f"Auto-fill analysis failed: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Auto-fill analysis failed: {str(e)}"
        )
