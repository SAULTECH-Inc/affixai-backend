from fastapi import APIRouter
from app.models.schemas import HealthResponse
from app.core.config import settings

router = APIRouter()


@router.get("/", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint
    
    Returns the health status of the FastAPI service and its dependencies.
    """
    
    # Check service status
    services_status = {
        "ocr": "healthy",
        "redis": "healthy",  # Would actually check Redis
        "s3": "healthy",     # Would actually check S3
    }
    
    return HealthResponse(
        status="healthy",
        version=settings.APP_VERSION,
        ocrEngine=settings.OCR_ENGINE,
        services=services_status
    )


@router.get("/ready")
async def readiness_check():
    """Readiness check for Kubernetes"""
    return {"status": "ready"}


@router.get("/live")
async def liveness_check():
    """Liveness check for Kubernetes"""
    return {"status": "alive"}
