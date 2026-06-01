"""
Arkon — Enterprise AI Control Center.
FastAPI application entry point.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import settings
from app.mcp.server import create_mcp_server

# Create the MCP server and its HTTP app (lifespan must be composed with FastAPI)
mcp_server = create_mcp_server()
mcp_http_app = mcp_server.http_app(path="/", stateless_http=True)


async def seed_default_admin():
    """Create default admin account from .env if no admin exists yet."""
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.database.models import Department, Employee, EmployeeDepartment
    from app.services.auth_service import hash_password

    try:
        async with async_session_factory() as session:
            # Check if any admin already exists
            stmt = select(Employee).where(Employee.role == "admin").limit(1)
            result = await session.execute(stmt)
            if result.scalar_one_or_none():
                return  # Admin already exists, skip

            # Create admin department
            dept = Department(name="Administration", description="System administrators")
            session.add(dept)
            await session.flush()

            # Create admin user from .env
            admin = Employee(
                name="Admin",
                email=settings.default_admin_email,
                password_hash=hash_password(settings.default_admin_password),
                role="admin",
            )
            session.add(admin)
            await session.flush()
            session.add(EmployeeDepartment(employee_id=admin.id, department_id=dept.id))
            await session.flush()

            await session.commit()
            logger.success(f"Default admin created: {settings.default_admin_email}")
    except Exception as e:
        logger.warning(f"Could not seed default admin: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup & shutdown logic (composed with FastMCP lifespan)."""
    async with mcp_http_app.lifespan(app):
        logger.info("Starting Arkon API...")

        # Ensure MinIO bucket exists
        try:
            from app.services.storage_service import storage_service
            await storage_service.ensure_bucket()
            logger.success("MinIO bucket ready")
        except Exception as e:
            logger.warning(f"MinIO not available yet: {e}")

        # Seed default admin if no admin exists yet
        await seed_default_admin()

        # Seed built-in skills (idempotent — no-op if already up to date)
        try:
            from app.scripts.seed_skills import seed_builtin_skills
            await seed_builtin_skills()
        except Exception as e:
            logger.warning(f"Could not seed built-in skills: {e}")

        # Warn if sensitive defaults are unchanged
        if settings.secret_key == "change-me-to-a-random-secret-string":
            logger.warning("⚠️  SECRET_KEY is set to the default value — change it before deploying to production!")
        if settings.default_admin_password == "admin123":
            logger.warning("⚠️  DEFAULT_ADMIN_PASSWORD is 'admin123' — change the admin password after first login!")

        # MCP server ready
        logger.success("Arkon MCP Server ready at /mcp")
        logger.success("Arkon API started successfully")
        yield

        logger.info("Arkon API shutdown complete")


app = FastAPI(
    title="Arkon API",
    description="Enterprise AI Control Center — Knowledge Base & Skill Management",
    version="0.1.0",
    lifespan=lifespan,
)

# --- CORS ---
logger.info(f"Allowed CORS origins: {settings.cors_origin_list}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- MCP OAuth discovery gate ---
# RFC 9728: when a request hits the protected resource (/mcp) without a
# Bearer token, return 401 with a WWW-Authenticate header pointing at the
# OAuth Protected Resource Metadata. This is the signal Claude Desktop (and
# other MCP clients) look for to auto-discover the OAuth flow and surface a
# "Connect" button instead of requiring the user to paste a Bearer token by
# hand.
#
# Behavior:
#   - No Authorization header at all  -> 401 + WWW-Authenticate (triggers OAuth)
#   - Authorization: Bearer <token>   -> pass through (FastMCP + tool-level
#                                        checks handle validity / scope)
#
# We intentionally do NOT validate the token here. Invalid tokens still flow
# into the tool layer so existing helpful error messages are preserved.

@app.middleware("http")
async def _mcp_oauth_gate_mw(request, call_next):
    path = request.url.path
    if path == "/mcp" or path.startswith("/mcp/"):
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            base = str(request.base_url).rstrip("/")
            resource_metadata_url = f"{base}/.well-known/oauth-protected-resource"
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "error_description": (
                        "This MCP endpoint requires OAuth 2.0 or a Bearer token. "
                        "See the WWW-Authenticate header for OAuth discovery."
                    ),
                },
                headers={
                    "WWW-Authenticate": (
                        f'Bearer realm="Arkon MCP", '
                        f'resource_metadata="{resource_metadata_url}"'
                    ),
                },
            )
    return await call_next(request)


# --- Notification dispatch middleware ---
# Initialise the per-request notification staging bucket at the start of every
# HTTP request; flush to external channels (email, webhook) after the response
# is built. Fire-and-forget — errors are swallowed by the dispatcher.

@app.middleware("http")
async def _notification_dispatch_mw(request, call_next):
    from app.services import notification_service
    notification_service.init_request_dispatch_scope()
    response = await call_next(request)
    try:
        await notification_service.dispatch_pending()
    except Exception as e:  # pragma: no cover — defensive, dispatcher already catches
        logger.warning(f"Notification dispatch middleware failed: {e}")
    return response

# --- Mount MCP Server ---
# Claude Desktop connects to: https://your-server/mcp
app.mount("/mcp", mcp_http_app)

# --- REST API Routers ---
from app.routers import (  # noqa: E402
    admin_embeddings,
    admin_models,
    admin_settings,
    admin_stats,
    audit,
    auth,
    knowledge_types,
    notes,
    notifications,
    oauth,
    projects,
    rbac,

    skill_contributions,
    skills,
    sources,
    wiki,
    wiki_branches,
    wiki_drafts,
    wiki_images,
)

app.include_router(oauth.wellknown_router)
app.include_router(oauth.router, prefix="/oauth", tags=["oauth"])
app.include_router(auth.router, prefix="/api", tags=["auth"])
app.include_router(sources.router, prefix="/api", tags=["sources"])
app.include_router(notes.router, prefix="/api", tags=["notes"])
app.include_router(wiki_branches.router, prefix="/api", tags=["wiki-branches"])
app.include_router(wiki_drafts.router, prefix="/api", tags=["wiki-drafts"])
app.include_router(wiki.router, prefix="/api", tags=["wiki"])
app.include_router(wiki_images.router, prefix="/api", tags=["wiki"])
app.include_router(admin_settings.router, prefix="/api", tags=["settings"])
app.include_router(admin_embeddings.router, prefix="/api", tags=["settings"])
app.include_router(admin_models.router, prefix="/api", tags=["settings"])
app.include_router(admin_stats.router, prefix="/api", tags=["statistics"])
app.include_router(rbac.router, prefix="/api", tags=["rbac"])
app.include_router(knowledge_types.router, prefix="/api", tags=["knowledge-types"])
app.include_router(projects.router, prefix="/api", tags=["projects"])

app.include_router(audit.router, prefix="/api", tags=["audit"])
app.include_router(skills.router, prefix="/api", tags=["skills"])
app.include_router(skill_contributions.router, prefix="/api", tags=["skill-contributions"])
app.include_router(notifications.router, prefix="/api", tags=["notifications"])


@app.get("/")
async def root():
    return {
        "name": "Arkon",
        "description": "Enterprise AI Control Center",
        "version": "0.1.0",
        "mcp_endpoint": "/mcp",
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    services = {}
    overall = "healthy"

    # Database
    try:
        from sqlalchemy import text

        from app.database import async_session_factory
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        services["database"] = "healthy"
    except Exception as e:
        services["database"] = "error"
        overall = "degraded"
        logger.warning(f"Health check — database error: {e}")

    # Redis
    try:
        from app.routers.sources import get_arq_pool
        pool = await get_arq_pool()
        await pool.ping()
        services["redis"] = "healthy"
    except Exception as e:
        services["redis"] = "error"
        overall = "degraded"
        logger.warning(f"Health check — redis error: {e}")

    # MinIO
    try:
        from app.services.storage_service import storage_service
        await storage_service.ensure_bucket()
        services["minio"] = "healthy"
    except Exception as e:
        services["minio"] = "error"
        overall = "degraded"
        logger.warning(f"Health check — minio error: {e}")

    return {"status": overall, "services": services}


@app.get("/api/health")
async def api_health():
    """Detailed health check for API, database, and worker (Redis)."""
    from sqlalchemy import text

    from app.database import async_session_factory

    result = {
        "api": "healthy",
        "database": "error",
        "worker": "error",
    }

    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        result["database"] = "healthy"
    except Exception as e:
        logger.warning(f"Health check: DB error — {e}")

    try:
        import redis.asyncio as aioredis
        r = aioredis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            db=settings.redis_db,
            socket_connect_timeout=2,
        )
        await r.ping()
        await r.aclose()
        result["worker"] = "healthy"
    except Exception as e:
        logger.warning(f"Health check: Redis error — {e}")

    return result
