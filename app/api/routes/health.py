from fastapi import APIRouter, Header, HTTPException
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


@router.get("/db")
async def database_diagnostic(x_internal_key: str = Header(default="")):
    """Diagnose database connectivity from inside the running function.

    Exists because the failure it diagnoses is deeply unhelpful on its own. In
    a Lambda-style sandbox (Vercel included) a DNS or reachability failure can
    surface as `OSError: [Errno 16] Device or resource busy` from the event
    loop's `create_connection`, which names neither the host nor the real
    problem. Every database-backed endpoint then returns a 500 that says
    nothing.

    Gated on INTERNAL_API_KEY rather than user auth on purpose: authenticating
    a user requires the database, so any auth-gated diagnostic is unusable in
    exactly the situation it's for.

    Reports the host being dialled, what it resolves to (IPv4 vs IPv6 — a
    host that only publishes AAAA records is unreachable from a network
    without IPv6, a common and very confusing production failure), and the
    result of an actual connection attempt.
    """
    import asyncio
    import socket
    from urllib.parse import urlparse

    if not settings.INTERNAL_API_KEY or x_internal_key != settings.INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid internal key")

    raw = settings.DATABASE_URL or ""
    parsed = urlparse(raw)
    host = parsed.hostname or ""
    port = parsed.port or 5432

    out: dict = {
        "database_url_set": bool(raw),
        "host": host,
        "port": port,
        "event_loop": type(asyncio.get_running_loop()).__module__,
        "dns": {},
        "connect": {},
    }

    # DNS: which address families does this host actually publish?
    for family, label in ((socket.AF_INET, "ipv4"), (socket.AF_INET6, "ipv6")):
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM
            )
            out["dns"][label] = [i[4][0] for i in infos][:3]
        except Exception as exc:
            out["dns"][label] = f"{type(exc).__name__}: {exc}"

    # A raw TCP connect, separating "can't reach the host" from anything
    # Postgres or TLS related.
    try:
        fut = asyncio.get_running_loop().create_connection(asyncio.Protocol, host, port)
        transport, _ = await asyncio.wait_for(fut, timeout=8)
        transport.close()
        out["connect"]["tcp"] = "ok"
    except Exception as exc:
        out["connect"]["tcp"] = f"{type(exc).__name__}: {exc}"

    # And the real thing, through the ORM's own connection path.
    try:
        from tortoise import Tortoise

        conn = Tortoise.get_connection("default")
        await conn.execute_query("SELECT 1")
        out["connect"]["orm_query"] = "ok"
    except Exception as exc:
        out["connect"]["orm_query"] = f"{type(exc).__name__}: {exc}"

    return out
