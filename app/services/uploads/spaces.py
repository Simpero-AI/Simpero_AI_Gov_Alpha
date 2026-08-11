"""Spaces (S3-compatible) adapter for document uploads (SIM-220/216/218).

Reuses the parser service's bucket (PARSER_SPACES_BUCKET / SPACES_* here --
see app/core/config.py and docs/plans/document-upload-presigned-urls.md's
"Storage / Spaces configuration" section) rather than provisioning a new one.
Org isolation for the shared bucket comes from the object key being derived
deterministically from the authenticated caller's own org, not from any IAM
condition -- a presigned PUT URL is signed for one exact key, not a prefix.

boto3 is already a dependency (pyproject.toml) -- kept for exactly this.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from uuid import UUID

import boto3
from botocore.exceptions import ClientError

from app.core.config import get_settings

# Bounds peak memory for stream_and_hash regardless of object size -- never
# read the whole object into memory at once.
_CHUNK_SIZE = 64 * 1024

# S3-safe key characters; anything else in org_name is replaced with "_"
# before it goes into a key (implementer detail per the plan, not a design
# decision -- see build_object_key).
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class ObjectTooLargeError(Exception):
    """Raised by stream_and_hash when the object exceeds max_bytes before the
    stream finishes reading. The caller (Phase 5's ingest job) is expected to
    catch this and mark the row `quarantined` without ever computing a
    fingerprint for the rejected object.
    """


@lru_cache
def _client():
    # Lazy singleton, same idiom as app/jobs/queue.py::get_queue and
    # app/jobs/parse_client.py::get_parse_queue -- no connection opens until
    # the first real call.
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.spaces_endpoint_url,
        region_name=settings.spaces_region,
        aws_access_key_id=settings.spaces_access_key_id,
        aws_secret_access_key=settings.spaces_secret_access_key,
    )


def build_object_key(
    org_name: str, clerk_org_id: str, deal_id: UUID, upload_id: UUID, filename: str
) -> str:
    """Deterministic object key, never trusted from the client -- both
    /presigned-url and /complete derive it independently from the same
    inputs. `upload_id` is prepended to `filename` to avoid a collision if
    two uploads in the same deal share a filename.
    """
    safe_org_name = _UNSAFE_KEY_CHARS.sub("_", org_name)
    return f"{safe_org_name}-{clerk_org_id}/{deal_id}/{upload_id}-{filename}"


def presign_put(key: str, ttl_seconds: int) -> str:
    settings = get_settings()
    return _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.spaces_bucket, "Key": key},
        ExpiresIn=ttl_seconds,
    )


def head_object(key: str) -> bool:
    """Existence check used at /complete to confirm the presigned PUT
    actually happened before a data_source row is created. A missing object
    is an expected, non-exceptional outcome here -- only unexpected errors
    (auth, permissions, etc.) propagate.
    """
    settings = get_settings()
    try:
        _client().head_object(Bucket=settings.spaces_bucket, Key=key)
        return True
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def get_json_object(bucket: str, key: str) -> dict:
    """Fetch and parse a JSON object -- the claims envelope
    Simpero_Gov_AI_Services' process_document job writes, per the
    `{bucket, key}` pointer in its result. `bucket` is taken from the
    pointer itself, not `settings.spaces_bucket` -- today they're always
    the same shared bucket, but the read-back should trust what the
    pointer names, not assume it.
    """
    response = _client().get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read())


def stream_and_hash(key: str, max_bytes: int | None = None) -> str:
    """Chunked GET + SHA-256 over the object body, reading in fixed
    _CHUNK_SIZE reads -- never buffers the whole object in memory.

    `max_bytes`, if given, is a hard ceiling: reading stops (and
    ObjectTooLargeError is raised) as soon as bytes read exceeds it, rather
    than reading an arbitrarily large object to completion first. This is
    Phase 5's 10 MB enforcement backstop -- the point of bailing early is to
    avoid paying the cost of hashing an object that's going to be rejected
    anyway, so a fingerprint is never computed for an oversized object.
    """
    settings = get_settings()
    response = _client().get_object(Bucket=settings.spaces_bucket, Key=key)
    body = response["Body"]  # botocore StreamingBody
    digest = hashlib.sha256()
    bytes_read = 0
    while True:
        chunk = body.read(_CHUNK_SIZE)
        if not chunk:
            break
        bytes_read += len(chunk)
        if max_bytes is not None and bytes_read > max_bytes:
            raise ObjectTooLargeError(f"object {key!r} exceeds {max_bytes} bytes")
        digest.update(chunk)
    return digest.hexdigest()
