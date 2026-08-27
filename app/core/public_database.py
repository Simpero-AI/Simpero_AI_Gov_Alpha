from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

settings = get_settings()

# Same reasoning as app/core/database.py's dd_app engine: PgBouncer IS the
# connection pool under transaction-mode pooling, so NullPool is required
# here too. This is a SEPARATE engine bound to the dd_public role -- never
# imported by app/core/dependencies.py or any admin/product router. The only
# importer is app/core/public_dependencies.py (added in P1-04).
public_engine = create_async_engine(
    settings.public_database_url,
    poolclass=NullPool,
    echo=settings.environment == "development",
)

PublicAsyncSessionLocal = async_sessionmaker(
    public_engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)
