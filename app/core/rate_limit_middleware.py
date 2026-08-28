import logging
from typing import cast

import redis.exceptions
from fastapi import Request
from fastapi.responses import JSONResponse
from redis.asyncio.client import Redis
from saq.queue.redis import RedisQueue
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.jobs.queue import get_queue

logger = logging.getLogger(__name__)

IP_LIMIT = 5
IP_WINDOW_SECONDS = 10


async def check_rate_limit(redis_client: Redis, key: str, limit: int, window_seconds: int) -> bool:
    """Fixed-window counter. SET NX EX (not bare INCR+EXPIRE) guarantees the
    key always carries a TTL from creation -- a crash between INCR and a
    separate EXPIRE call would otherwise leave the key permanent."""
    added = await redis_client.set(key, 1, nx=True, ex=window_seconds)
    count = 1 if added else await redis_client.incr(key)
    return count <= limit


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        last = forwarded_for.split(",")[-1].strip()
        if last:
            # Trust only the LAST entry -- in prod/staging only Caddy can
            # reach the app container, and Caddy's reverse_proxy appends the
            # real peer IP as the last entry. Earlier entries could be
            # attacker-spoofed and must never be trusted for rate-limit keying.
            return last
    if request.client is None:
        return "unknown"
    return request.client.host


class RateLimitMiddleware(BaseHTTPMiddleware):
    """IP-based fixed-window throttle on the public intake surface. Defense
    in depth alongside the per-link DB lockout (app/api/public_intake.py) --
    not the hard security boundary, so a Valkey outage fails OPEN rather than
    taking down the whole public intake surface.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if not (path == "/api/public" or path.startswith("/api/public/")):
            return await call_next(request)

        ip = _client_ip(request)
        key = f"ratelimit:ip:{ip}"
        try:
            # get_queue() is statically typed as the abstract saq.Queue (it
            # dispatches on url scheme at runtime); VALKEY_URL is always
            # redis://, so it's always a RedisQueue with a `.redis` client.
            redis_client = cast(RedisQueue, get_queue()).redis
            allowed = await check_rate_limit(redis_client, key, IP_LIMIT, IP_WINDOW_SECONDS)
        except redis.exceptions.RedisError:
            logger.warning("Rate limit check failed (Valkey error); failing open", exc_info=True)
            return await call_next(request)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too Many Requests"},
                headers={"Retry-After": str(IP_WINDOW_SECONDS)},
            )

        return await call_next(request)
