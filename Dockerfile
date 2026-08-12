# Stage 1: obtain the uv binary from the official distroless image.
# This avoids installing uv via pip (contradicts the no-pip rule) or curl (not reproducible).
FROM ghcr.io/astral-sh/uv:latest AS uv-binary

# Stage 2: install dependencies using uv into a virtualenv.
FROM python:3.11-slim AS builder

COPY --from=uv-binary /uv /usr/local/bin/uv

WORKDIR /app

# Copy only dependency manifests first — Docker layer cache skips re-install if they haven't changed.
COPY pyproject.toml ./
# uv.lock may not exist yet on first build; COPY with wildcard handles both cases.
COPY uv.loc[k] ./

# --frozen: respect uv.lock exactly, no version resolution (reproducible builds).
# --no-dev: skip dev dependencies in the production image.
RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

# Stage 3: minimal runtime image.
FROM python:3.11-slim AS runtime

# Do not run as root.
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Copy installed virtualenv from builder (not uv itself — not needed at runtime).
COPY --from=builder /app/.venv /app/.venv

# Copy application source.
COPY app/ /app/app/

# alembic.ini (script_location = alembic, prepend_sys_path = .) and the
# migration scripts themselves — needed for `alembic upgrade head` to run
# inside this image (see deploy/docker-compose.prod.yml's migration step).
COPY alembic.ini ./
COPY alembic/ /app/alembic/

# app/jobs/tasks/start_deal_verification.py reads
# contracts/claims.schema.json at runtime (Path(__file__).parents[3], i.e.
# /app/contracts/... from inside this image) to validate claims/edges before
# ingest -- omitted here until caught by an actual local run of that job
# (FileNotFoundError), since app/'s own test suite never exercises the built
# image, only the source tree directly.
COPY contracts/ /app/contracts/

# Activate the virtualenv.
ENV PATH="/app/.venv/bin:$PATH"

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
