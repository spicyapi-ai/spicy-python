"""Official SpicyAPI client for Python 3.11+.

SpicyAPI serves image, video and audio generation models behind one API. Text
models are not covered on purpose: they speak the OpenAI, Anthropic and Gemini
wire formats, so the official libraries for those already work against this
service. The upload helpers here still apply to them: an audio part in a chat
message accepts only an uploaded ``spicy://f/...`` URI, and video is reliable
only as one.

Depends on the standard library only. The ten-minute polling deadline is a local
safety bound, not a service guarantee.

It covers the whole media workflow: discover a model, upload a reference file,
confirm the price, create the task, wait for a terminal state, read the result,
refresh an expired output link, and destroy the stored content afterwards.
Files worth keeping go into the asset library (``client.assets``), where they
get a stable ``spicy://a/`` reference that does not expire.
Webhook signature verification is a module-level function so a web handler can
use it without constructing a client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

API_BASE_URL = "https://api.spicyapi.ai/api/v1"
REQUEST_TIMEOUT_SECONDS = 30.0
# Sending bytes is not an API call. 90 MiB in 30 seconds would demand a
# sustained 25 Mbit/s uplink, and the failure reads as a network fault when it
# is really this client hanging up on itself.
UPLOAD_TIMEOUT_SECONDS = 10.0 * 60.0
WAIT_TIMEOUT_SECONDS = 10.0 * 60.0
TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled", "expired"})
ACTIVE_STATES = frozenset({"queued", "running"})
# The single source of the version. It lives here rather than in __init__.py because __init__
# imports this module, so reading it the other way round would be a circular import; and the tests
# load this file as a standalone module with no parent package, where a relative import is bound to
# fail. Both __init__.py and pyproject read it from here.
__version__ = "0.3.0"

# Local ceiling on response bodies. See _read_capped: this number is ours, not the contract's.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 64 * 1024

RETRYABLE_HTTP = frozenset({408, 429, 500, 502, 503, 504})
RETRYABLE_CODES = frozenset({429, 500, 50301})
# 50302 rides on a 503, so anything looking only at the HTTP status retries it as ordinary
# upstream unavailability. The contract is explicit: this idempotency key has already recorded the
# failure, and reusing it only replays that failure - four attempts, all four doomed, and a final
# error that reads as "we retried and it still failed", burying the actual remedy (send again under
# a new key). This set is consulted ahead of the status code.
NON_RETRYABLE_CODES = frozenset({50302})

IMAGE_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
AUDIO_VIDEO_CONTENT_TYPES = frozenset({"video/mp4", "video/webm", "audio/mpeg", "audio/wav"})
# Reference documents, for models whose schema takes a file field (for example generating a video
# from a brief). The platform checks the file signature and the size, never the content.
DOCUMENT_CONTENT_TYPES = frozenset({
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.apple.keynote",
    "application/vnd.apple.pages",
    "application/vnd.apple.numbers",
    "text/plain",
    "text/markdown",
})
# LoRA weights, for ``loras[].path``: a ``.safetensors`` file, and commit checks that it is
# complete. The contract names no narrower type for it, so it travels as application/octet-stream.
LORA_CONTENT_TYPES = frozenset({"application/octet-stream"})
UPLOAD_CONTENT_TYPES = (
    IMAGE_CONTENT_TYPES | AUDIO_VIDEO_CONTENT_TYPES | DOCUMENT_CONTENT_TYPES | LORA_CONTENT_TYPES
)
MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024
# Documents share the audio and video ceiling: a PDF or a slide deck with pictures in it is often
# larger than any single image, and the platform signs both against the same 90 MiB line.
MAX_AUDIO_VIDEO_UPLOAD_BYTES = 90 * 1024 * 1024
MAX_DOCUMENT_UPLOAD_BYTES = MAX_AUDIO_VIDEO_UPLOAD_BYTES
# LoRA weights have a ceiling of their own. A file this size can outlast the default ten-minute
# upload timeout on an ordinary uplink, so raise upload_timeout_seconds for big ones; the ticket
# itself stays valid for two hours.
MAX_LORA_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
CONTENT_TYPE_BY_SUFFIX = {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".key": "application/vnd.apple.keynote",
    ".markdown": "text/markdown",
    ".md": "text/markdown",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".numbers": "application/vnd.apple.numbers",
    ".pages": "application/vnd.apple.pages",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".safetensors": "application/octet-stream",
    ".txt": "text/plain",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

WEBHOOK_TOLERANCE_SECONDS = 300
WEBHOOK_MAX_BODY_BYTES = 1024 * 1024

# Prefix of a saved asset's stable reference, as in ``spicy://a/ast_...``.
ASSET_URI_PREFIX = "spicy://a/"
# Only used to cut the default name of an uploaded file; a name passed explicitly is the server's
# to judge.
ASSET_NAME_MAX_CHARS = 80
# Orders of an asset listing. An unknown sort is refused here rather than spending a request, and a
# slice of the per-minute read limit, on a 400.
ASSET_SORTS = frozenset({"recent", "oldest", "name", "size"})
ASSET_SOURCES = frozenset({"task_output", "upload", "url"})
# Machine-readable reasons the asset library attaches to a failed response as ``data.reason``:
#
# - asset_library_locked (400): the library opens after the owner's first top-up.
# - asset_library_full (400): the allowance's storage or item count is used up.
# - asset_file_too_large (400): the file is above the plan's per-file limit.
# - asset_daily_limit (429): today's (UTC) allowance of new assets is used up; see Retry-After.
# - asset_storage_full (503): storage is temporarily full across the platform.
# - asset_permission_denied (403): the key's team role does not allow this change.
# - asset_workspace_forbidden (403): this key or workspace cannot reach that library.
# - asset_entity_name_taken (409): the library already has a character with that name, ignoring
#   case. Characters are named in the console, so the read-only API does not return it today.
#
# The set matches the published contract (scripts/check_contract.py and a unit test compare them).
# The server may add reasons later, so treat it as open: SpicyApiError.reason carries any string the
# server sends, unchanged, and an unknown one is best handled like an absent reason.
ASSET_ERROR_REASONS = frozenset({
    "asset_library_locked",
    "asset_library_full",
    "asset_file_too_large",
    "asset_daily_limit",
    "asset_storage_full",
    "asset_permission_denied",
    "asset_workspace_forbidden",
    "asset_entity_name_taken",
})

# What to do about a business code, straight from the published contract. These
# are the cases where blind retrying is either useless or wrong.
RECOVERY_BY_CODE: Mapping[int, str] = {
    40003: (
        "the stored bytes do not match the upload ticket (size, media type or signature): "
        "request a new ticket with the exact contentType and byte count, PUT the file again, "
        "then commit. Retrying the commit alone cannot change the stored object"
    ),
    40004: (
        "the request is valid but no deployment serves this parameter combination: change the "
        "parameter named in the message, or pick another model. The identical request fails again"
    ),
    40310: (
        "the account has not verified its email address yet, so it cannot run models: open the "
        "verification link sent at sign-up, or send a new one from the Console. The same key "
        "works as soon as the address is verified; resending before that is refused again"
    ),
    40901: (
        "the quote expired or the price changed before the funds were reserved: quote again and "
        "resend with the new quoteId and expectedCost. Keep the original Idempotency-Key so an "
        "already accepted task is recovered instead of created twice"
    ),
    50301: (
        "the model has no usable deployment or effective price right now: retry later with "
        "backoff, or choose another model"
    ),
    50302: (
        "a synchronous generation failed upstream and the charge was refunded: the request can be "
        "sent again, with a new Idempotency-Key so it is treated as a fresh submission"
    ),
    503: (
        "a dependency is temporarily unavailable: wait for retry_after_seconds when the server "
        "sent one, then retry"
    ),
}

TransportResult = tuple[int, Mapping[str, str], bytes]
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], TransportResult]


class SpicyApiError(RuntimeError):
    """HTTP, envelope, or network failure with support correlation fields.

    ``reason`` is the failure envelope's ``data.reason`` when the server sent one, and None
    otherwise. Branch on it (or on ``code``) rather than on the message, which follows the
    account's error language. See ASSET_ERROR_REASONS for the values in use today; an unknown value
    is passed through as received.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: int | None = None,
        request_id: str = "",
        retry_after_seconds: float | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds
        self.reason = reason

    @property
    def recovery(self) -> str:
        """Contract-defined recovery step, or an empty string when there is none.

        Branch on ``code``, never on the message: the message is prose and is
        translated according to the account's API error language.
        """
        return RECOVERY_BY_CODE.get(self.code, "") if self.code is not None else ""


class SpicyUploadError(SpicyApiError):
    """The presigned PUT to object storage failed.

    Storage answers with its own status and an XML body rather than the
    SpicyAPI envelope, so there is no business ``code`` to branch on. A 403
    here usually means a ticket header was altered or dropped: both
    ``Content-Type`` and ``Content-Length`` are part of the signature.
    """


class SpicyTimeoutError(TimeoutError):
    """A local request or polling deadline elapsed; remote state is unknown."""

    def __init__(self, message: str, *, task_id: str | None = None) -> None:
        super().__init__(message)
        self.task_id = task_id


class SpicyWebhookError(ValueError):
    """A webhook delivery failed verification and must not be acted on."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _read_capped(stream: Any, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """Read at most ``limit`` bytes, raising instead of reading on forever.

    ``read()`` takes whatever the other end cares to send. A broken intermediary, or a hijacked
    connection, can emit bytes until the process is killed by the OOM reaper - and nothing along
    the way raises anything.

    The ceiling is not part of the contract; it is an engineering number of our own. Measured on
    2026-09-20, the public catalogue's 121 endpoints came to 273 KiB in total, and the internal
    catalogue with inputSchema is estimated at roughly 1.33 MiB, so 8 MiB leaves several times over
    for growth. 8 was chosen to match the Go client (the TypeScript one is currently 4 MiB).
    """
    chunks: list[bytes] = []
    read = 0
    while True:
        chunk = stream.read(_READ_CHUNK)
        if not chunk:
            break
        read += len(chunk)
        if read > limit:
            raise SpicyApiError(
                f"response body exceeded the local {limit}-byte ceiling; "
                "refusing to buffer an unbounded response"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> TransportResult:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), _read_capped(response)
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), _read_capped(error)


def is_terminal(task: Mapping[str, Any]) -> bool:
    """Whether this task has reached a terminal state and will not change again.

    Branch on this whenever a task is created with ``wait_seconds``: what comes back may be a
    terminal record, or it may be the acceptance response returned once the wait budget ran out.
    The two have the same shape and differ only in ``state``, so the assumption "I passed wait, so
    it must be finished" silently carries a still-running task forward.
    """
    return task.get("state") in TERMINAL_STATES


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Look up a response header without regard to case.

    HTTP header names are case-insensitive, and urllib preserves whatever casing the server sent.
    A literal lookup misses ``retry-after`` when the server spells it that way - back-off then
    quietly falls back to local exponential timing and stops honouring the window the server asked
    for. Nothing raises; it just disobeys.
    """
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _normalise_base_url(value: str, label: str = "base_url") -> str:
    """Validate and normalise a base URL.

    Not validating means sending the Bearer key in the clear: point this at an http:// address and
    the key travels over the network, with no exception raised on the caller's side. The
    TypeScript, Go, PHP and Java clients all validate; this one used not to. http on a loopback
    address is allowed, because local development needs it.
    """
    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"{label} must be an absolute URL")
    host = parsed.hostname or ""
    loopback = host in {"localhost", "::1"} or host == "127.0.0.1" or host.startswith("127.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError(
            f"{label} must use HTTPS; HTTP is allowed only for loopback development"
        )
    if parsed.username or parsed.password:
        raise ValueError(f"{label} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain a query or fragment")
    return value.rstrip("/")


def _user_agent() -> str:
    """``spicyapi-python/<version>``, with a single source for the version.

    Without this header urllib sends its own default, ``Python-urllib/3.x``. That costs more than
    version telemetry: as scripts/check_contract.py in this repository records, the docs site's
    edge protection answers 403 to urllib's default UA. Were the same rule ever applied to the api
    host, every Python user would get a 403 at once - and nobody debugging it would think of the
    User-Agent.
    """
    return f"spicyapi-python/{__version__}"


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header, or None when it is absent or unusable."""
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def output_text(task: Mapping[str, Any]) -> str | None:
    """The text answer of a finished task, or None when it produced files.

    Some endpoints answer in ``output.text`` and carry no ``assets`` key at
    all, so reading ``task["output"]["assets"]`` directly raises KeyError on a
    perfectly successful task. Speech-to-text is one of them: this is the plain
    transcript, and ``output_transcript`` returns the timed version.
    """
    output = task.get("output")
    if not isinstance(output, Mapping):
        return None
    text = output.get("text")
    return text if isinstance(text, str) else None


def output_transcript(task: Mapping[str, Any]) -> dict[str, Any] | None:
    """The structured transcript of a speech-to-text task, or None.

    Carries ``text``, the detected ``language`` and ``languageCode``,
    ``durationSeconds``, ``words`` (each with ``text``, ``start`` and ``end`` in
    seconds, plus ``speakerId`` when the model separates speakers) and, for a
    multichannel request, ``channels``. Only some transcription endpoints return
    it, and it is erased with the rest of the output on the retention schedule,
    so None is an ordinary answer: fall back to ``output_text`` for the plain
    transcript.
    """
    output = task.get("output")
    if not isinstance(output, Mapping):
        return None
    transcript = output.get("transcript")
    return dict(transcript) if isinstance(transcript, Mapping) else None


def output_assets(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Generated files of a finished task, or an empty list.

    Returns [] both for a text answer and for a failed task, so callers never
    have to distinguish "no assets key" from "empty assets list". Every key of
    an asset is kept as received, including ``layer`` (``zIndex``, ``name``,
    ``boundingBox``) on layer decomposition results.
    """
    output = task.get("output")
    if not isinstance(output, Mapping):
        return []
    assets = output.get("assets")
    if not isinstance(assets, list):
        return []
    return [dict(asset) for asset in assets if isinstance(asset, Mapping)]


def content_type_for_path(path: str) -> str:
    """Upload content type inferred from a file name extension."""
    suffix = os.path.splitext(path)[1].lower()
    content_type = CONTENT_TYPE_BY_SUFFIX.get(suffix)
    if content_type is None:
        raise ValueError(
            f"cannot infer an upload content type from {path!r}; "
            f"pass content_type explicitly, one of {sorted(UPLOAD_CONTENT_TYPES)}"
        )
    return content_type


def max_upload_bytes(content_type: str) -> int:
    """Platform ceiling for this media type. A model's schema may allow less."""
    if content_type in IMAGE_CONTENT_TYPES:
        return MAX_IMAGE_UPLOAD_BYTES
    if content_type in DOCUMENT_CONTENT_TYPES:
        return MAX_DOCUMENT_UPLOAD_BYTES
    if content_type in LORA_CONTENT_TYPES:
        return MAX_LORA_UPLOAD_BYTES
    return MAX_AUDIO_VIDEO_UPLOAD_BYTES


def is_asset_error_reason(value: object) -> bool:
    """Whether ``value`` is one of the asset library reasons the contract lists today."""
    return isinstance(value, str) and value in ASSET_ERROR_REASONS


def _failure_reason(data: object) -> str | None:
    """``data.reason`` of a failure envelope, or None.

    Any non-empty string is kept verbatim, so a reason added on the server later still reaches the
    caller; anything else (absent, empty, not a string) reads as no reason at all.
    """
    if not isinstance(data, Mapping):
        return None
    reason = data.get("reason")
    return reason if isinstance(reason, str) and reason else None


def _require_identifier(value: object, label: str) -> str:
    """Trim a caller-supplied identifier and refuse values that cannot be sent as-is.

    These checks run before any request: an empty id would otherwise address the collection
    (``/assets/`` lists instead of reading one asset), and a line break has no business in a URL or
    a header.
    """
    if value is None:
        raise ValueError(f"{label} is required")
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    normalised = value.strip()
    if not normalised:
        raise ValueError(f"{label} is required")
    if "\r" in normalised or "\n" in normalised:
        raise ValueError(f"{label} must not contain line breaks")
    return normalised


def _require_text(value: object, label: str) -> str:
    """A required free-form value (an output key, a URL): present and not blank, sent unchanged.

    Unlike an identifier it is not trimmed, because whitespace inside it may be meaningful and the
    server is the one to judge its shape.
    """
    if value is None:
        raise ValueError(f"{label} is required")
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not value.strip():
        raise ValueError(f"{label} must not be blank")
    return value


def _bare_asset_id(value: object, label: str = "asset_id") -> str:
    """Accept either the bare id (``ast_...``) or the reference used in model inputs
    (``spicy://a/ast_...``), so a value copied from either place works."""
    trimmed = _require_identifier(value, label)
    if trimmed.startswith(ASSET_URI_PREFIX):
        return _require_identifier(trimmed[len(ASSET_URI_PREFIX):], label)
    return trimmed


def _asset_path(value: object) -> str:
    return f"/assets/{urllib.parse.quote(_bare_asset_id(value), safe='')}"


def _optional_text(value: str | None, label: str) -> str | None:
    """A filter that is either left off (None) or a non-empty string.

    An empty string is not "unset": the caller computed a filter and it came out empty. Dropping it
    silently would hand back the unfiltered list; sending it earns a 400 that does not name the
    parameter. Refuse it here by name, as list_tasks does.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if value == "":
        raise ValueError(f"{label} must not be an empty string; omit it to leave that filter off")
    return value


def _optional_limit(limit: int | None) -> str | None:
    # Only non-positive values are refused: they are unambiguously a caller error. The upper bound
    # is the server's, for the same reason as in list_tasks.
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    return str(limit)


def _with_query(path: str, values: Mapping[str, str | None]) -> str:
    present = {key: value for key, value in values.items() if value is not None}
    return f"{path}?{urllib.parse.urlencode(present)}" if present else path


def _every_page(
    first_cursor: str | None,
    fetch_page: Callable[[str | None], Mapping[str, Any]],
    what: str,
) -> Iterator[dict[str, Any]]:
    """Follow ``nextCursor`` until the last page, where it is absent.

    A cursor that comes back twice would loop forever and burn the rate limit for nothing, so it is
    refused.
    """
    seen: set[str] = set()
    cursor = first_cursor
    while True:
        page = fetch_page(cursor)
        items = page.get("items") if isinstance(page, Mapping) else None
        yield from items or []
        next_cursor = page.get("nextCursor") if isinstance(page, Mapping) else None
        if not isinstance(next_cursor, str) or not next_cursor:
            return
        if next_cursor in seen:
            raise SpicyApiError(f"{what} returned a cursor it had already returned", status=200)
        seen.add(next_cursor)
        cursor = next_cursor


def compute_webhook_signature(
    task_id: str,
    timestamp: str | int,
    raw_body: bytes,
    secret: str,
) -> str:
    """Base64 HMAC-SHA256 over ``taskId.timestamp.hex(sha256(raw_body))``.

    ``raw_body`` must be the exact bytes received. Re-serializing the parsed
    JSON changes key order and whitespace, and the signature no longer matches.
    """
    digest = hashlib.sha256(raw_body).hexdigest()
    message = f"{task_id}.{timestamp}.{digest}".encode()
    mac = hmac.new(secret.encode(), message, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def verify_webhook(
    *,
    raw_body: bytes,
    timestamp: str,
    signature: str,
    payload_version: int | str,
    secret: str,
    tolerance_seconds: int = WEBHOOK_TOLERANCE_SECONDS,
    now: Callable[[], float] = time.time,
    max_body_bytes: int = WEBHOOK_MAX_BODY_BYTES,
) -> dict[str, Any]:
    """Verify one ``callBackUrl`` delivery and return its parsed payload.

    This verifies the signature scheme used by the per-task ``callBackUrl``:
    ``base64(HMAC-SHA256(secret, "taskId.timestamp.hex(sha256(body))"))``, carried
    in ``X-Webhook-Signature``. Naming it precisely matters because a second,
    account-level delivery scheme is planned; when that ships it gets its own
    verifier rather than extra arguments here, and code written today keeps working.

    ``timestamp``, ``signature`` and ``payload_version`` are the
    ``X-Webhook-Timestamp``, ``X-Webhook-Signature`` and
    ``X-Webhook-Payload-Version`` headers. Raises SpicyWebhookError with a
    machine-readable ``reason`` when the delivery must be rejected.

    Deliveries are retried, so a receiver must be idempotent: for payload
    version 2 the ``request_id`` field is the stable delivery identifier.
    """
    if len(raw_body) > max_body_bytes:
        raise SpicyWebhookError("body_too_large", f"webhook body exceeds {max_body_bytes} bytes")
    if not secret.strip():
        raise SpicyWebhookError("invalid_secret", "webhook signing secret is required")
    version = str(payload_version)
    if version not in {"1", "2"}:
        raise SpicyWebhookError("invalid_version", "webhook payload version must be 1 or 2")
    if not timestamp.isdigit():
        raise SpicyWebhookError("invalid_timestamp", "webhook timestamp must be Unix seconds")

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SpicyWebhookError("invalid_json", "webhook body is not valid JSON") from error
    if not isinstance(payload, dict):
        raise SpicyWebhookError("invalid_payload", "webhook body is not a JSON object")

    # The task ID is part of the signed string, and the two payload versions
    # keep it in different places.
    if version == "1":
        task_id = payload.get("task_id")
    else:
        data = payload.get("data")
        task_id = data.get("taskId") if isinstance(data, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise SpicyWebhookError(
            "invalid_payload",
            f"webhook payload version {version} does not contain a task ID",
        )

    expected = compute_webhook_signature(task_id, timestamp, raw_body, secret)
    # Constant time: a byte-by-byte comparison leaks how much of a forged
    # signature was correct, which is enough to construct a valid one.
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        raise SpicyWebhookError("invalid_signature", "webhook signature does not match")

    # Freshness is checked only after the signature: an unauthenticated body
    # should never decide anything, including whether it is too old.
    seconds = int(timestamp)
    if abs(int(now()) - seconds) > tolerance_seconds:
        raise SpicyWebhookError(
            "stale_timestamp",
            f"webhook timestamp is more than {tolerance_seconds}s from now",
        )

    delivery_id = payload.get("request_id") if version == "2" else None
    return {
        "payload": payload,
        "payload_version": int(version),
        "task_id": task_id,
        "delivery_id": delivery_id if isinstance(delivery_id, str) else None,
        "timestamp": seconds,
    }


class SpicyAssetCharacters:
    """The characters of the library the key reaches, read-only.

    A character is a named, ordered set of images arranged in the console. It references assets
    instead of copying them, so each image carries its own ``spicy://a/<id>`` ``uri``. To use a
    character in a generation request, put the ``uri`` of its images, in order, into the model's
    reference image field.

    Personas became characters in the personal library on 2026-10-12. Existing ``spicy://p/<id>``
    references keep resolving to the same images; new code should use the ``spicy://a/``
    references listed here.

    Reach it as ``client.assets.characters``; it is not meant to be constructed directly.
    """

    def __init__(self, client: SpicyClient) -> None:
        self._client = client

    def _list_query(
        self,
        *,
        asset_id: str | None,
        cursor: str | None,
        limit: int | None,
    ) -> dict[str, str | None]:
        return {
            "assetId": None if asset_id is None else _bare_asset_id(asset_id),
            "cursor": _optional_text(cursor, "cursor"),
            "limit": _optional_limit(limit),
        }

    def list(
        self,
        *,
        asset_id: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """One page of characters, newest first: ``items`` and, unless this is the last page,
        ``nextCursor``.

        ``asset_id`` (``ast_...`` or ``spicy://a/ast_...``) keeps only the characters that contain
        that asset. ``limit`` is the page size; the server defaults to 50. Continue with
        ``nextCursor``, or use iter_all.
        """
        query = self._list_query(asset_id=asset_id, cursor=cursor, limit=limit)
        return self._client._request("GET", _with_query("/assets/entities", query))

    def iter_all(
        self,
        *,
        asset_id: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Every character, page by page. ``limit`` sets the page size, not the total.

        The arguments are checked when this is called, not when iteration starts, so a bad value
        fails where it was written.
        """
        query = self._list_query(asset_id=asset_id, cursor=cursor, limit=limit)

        def fetch_page(page_cursor: str | None) -> Mapping[str, Any]:
            return self._client._request(
                "GET", _with_query("/assets/entities", {**query, "cursor": page_cursor})
            )

        return _every_page(query["cursor"], fetch_page, "character listing")

    def get(self, character_id: str) -> dict[str, Any]:
        """One character with its images in order, each with a fresh private ``previewUrl``.

        An unknown character, and one outside the key's library, are both answered with 404.
        """
        encoded = urllib.parse.quote(_require_identifier(character_id, "character_id"), safe="")
        return self._client._request("GET", f"/assets/entities/{encoded}")


class SpicyAssets:
    """The asset library the key reaches.

    That is the personal library for a personal key, and the team's shared library for a team key.
    Files are kept until they are deleted, each with a stable ``spicy://a/<id>`` reference that
    works in any media field of model inputs sent with keys of the same workspace. With a team key,
    the creator's current role decides what may change (see ``usage()["scope"]``).

    Reach it as ``client.assets``; it is not meant to be constructed directly. Responses never
    contain a public or permanent link: ``previewUrl`` is a private preview signed for at most 20
    minutes, so store ``uri``, never ``previewUrl``.

    Asset ids are accepted as ``ast_...`` or as the ``spicy://a/ast_...`` reference. Invalid
    arguments raise ValueError before any request is sent.
    """

    def __init__(self, client: SpicyClient) -> None:
        self._client = client
        #: Read-only access to the library's characters.
        self.characters = SpicyAssetCharacters(client)

    def create(
        self,
        *,
        source: str,
        task_id: str | None = None,
        key: str | None = None,
        index: int | None = None,
        file_id: str | None = None,
        url: str | None = None,
        name: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Save a file to the library and return the saved asset.

        Exactly one source per call:

        - ``source="task_output"`` with ``task_id`` and either the output ``key`` or its zero-based
          ``index`` (the order of ``output.assets`` in the task record). The task must be one of
          your own successful tasks; with a team key, one of the team's.
        - ``source="upload"`` with ``file_id``, a committed upload (``fil_...`` or
          ``spicy://f/fil_...``).
        - ``source="url"`` with ``url``, a public http or https address on port 80 or 443 without
          credentials. The server downloads it once and reads the type from the file itself.

        ``name`` is optional; the server defaults to "Saved asset". Saving the same source again
        returns the asset you already have and uses no extra space.

        The server requires an Idempotency-Key on every save. One is generated when
        ``idempotency_key`` is omitted, which also makes the transport-level retry safe: every
        attempt of this call carries the same key. Pass your own to replay the same save safely
        after an uncertain response, for example across a process restart; the server binds the
        key to the exact request for 24 hours.
        """
        if source not in ASSET_SOURCES:
            raise ValueError(f"source must be one of {sorted(ASSET_SOURCES)}, got {source!r}")
        given = {
            "task_id": task_id, "key": key, "index": index, "file_id": file_id, "url": url,
        }
        allowed = {
            "task_output": {"task_id", "key", "index"},
            "upload": {"file_id"},
            "url": {"url"},
        }[source]
        # A save has exactly one source, so a field that belongs to another one is a caller
        # mistake. The TypeScript client rules it out with its types; this is the runtime
        # equivalent.
        foreign = sorted(
            field for field, value in given.items() if value is not None and field not in allowed
        )
        if foreign:
            raise ValueError(
                f"{', '.join(foreign)} does not apply to source={source!r}; "
                "send exactly one source per save"
            )

        body: dict[str, Any] = {"source": source}
        if source == "task_output":
            body["taskId"] = _require_identifier(task_id, "task_id")
            if key is not None:
                body["key"] = _require_text(key, "key")
            if index is not None:
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    raise ValueError("index must be a non-negative integer")
                body["index"] = index
        elif source == "upload":
            body["fileId"] = _require_identifier(file_id, "file_id")
        else:
            body["url"] = _require_text(url, "url")
        if name is not None:
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            body["name"] = name

        header_key = (
            str(uuid.uuid4())
            if idempotency_key is None
            else _require_identifier(idempotency_key, "idempotency_key")
        )
        return self._client._request(
            "POST", "/assets", body=body, headers={"Idempotency-Key": header_key}
        )

    def _list_query(
        self,
        *,
        kind: str | None,
        source: str | Sequence[str] | None,
        q: str | None,
        favorite: bool | None,
        collection_id: str | None,
        entity_id: str | None,
        sort: str | None,
        cursor: str | None,
        limit: int | None,
    ) -> dict[str, str | None]:
        sources: str | None = None
        if isinstance(source, str):
            sources = _optional_text(source, "source")
        elif source is not None:
            values = [_optional_text(item, "source") for item in source]
            # An empty sequence filters nothing, so it is the same as leaving the filter off.
            sources = ",".join(value for value in values if value) or None
        if favorite is not None and not isinstance(favorite, bool):
            raise ValueError("favorite must be True, False or None")
        if sort is not None and sort not in ASSET_SORTS:
            raise ValueError(f"sort must be one of {sorted(ASSET_SORTS)}, got {sort!r}")
        return {
            "kind": _optional_text(kind, "kind"),
            "source": sources,
            "q": _optional_text(q, "q"),
            # False is a filter of its own ("not favorites"), so it is sent rather than dropped;
            # only None leaves the filter off.
            "favorite": None if favorite is None else ("true" if favorite else "false"),
            "collectionId": (
                None if collection_id is None
                else _require_identifier(collection_id, "collection_id")
            ),
            "entityId": None if entity_id is None else _require_identifier(entity_id, "entity_id"),
            "sort": sort,
            "cursor": _optional_text(cursor, "cursor"),
            "limit": _optional_limit(limit),
        }

    def list(
        self,
        *,
        kind: str | None = None,
        source: str | Sequence[str] | None = None,
        q: str | None = None,
        favorite: bool | None = None,
        collection_id: str | None = None,
        entity_id: str | None = None,
        sort: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """One page of ready assets: ``items`` and, unless this is the last page, ``nextCursor``.

        - ``kind``: ``image``, ``video`` or ``audio``.
        - ``source``: one of ``task_output``, ``upload`` and ``url``, or a list of them (sent
          comma-separated).
        - ``q``: a case-insensitive substring of the asset name.
        - ``favorite``: True lists only favorites, False only assets that are not favorites; None
          leaves the filter off.
        - ``collection_id``: only the assets in this folder (a collection of the same library).
          Folders are created and edited in the console; an id outside the key's library is
          answered with 404.
        - ``entity_id``: only the images of this character (see ``characters``); an unknown id
          gives an empty page.
        - ``sort``: ``recent`` (the default, newest first), ``oldest``, ``name`` (A to Z, ignoring
          case) or ``size`` (largest first).
        - ``cursor``: the previous page's ``nextCursor``, unchanged and with the same filters and
          ``sort``; never construct one. A cursor sent with a different ``sort`` is rejected with
          400, and that error is raised as-is rather than retried.
        - ``limit``: the page size; the server defaults to 100.

        Use iter_all to walk every page.
        """
        query = self._list_query(
            kind=kind, source=source, q=q, favorite=favorite, collection_id=collection_id,
            entity_id=entity_id, sort=sort, cursor=cursor, limit=limit,
        )
        return self._client._request("GET", _with_query("/assets", query))

    def iter_all(
        self,
        *,
        kind: str | None = None,
        source: str | Sequence[str] | None = None,
        q: str | None = None,
        favorite: bool | None = None,
        collection_id: str | None = None,
        entity_id: str | None = None,
        sort: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Every matching asset, page by page, following ``nextCursor`` until the last page.

        The same filters and ``sort`` go out on every page, as a cursor only works with the ones it
        was issued for. ``limit`` sets the page size, not the total; stop iterating to stop
        fetching. The arguments are checked when this is called, not when iteration starts.
        """
        query = self._list_query(
            kind=kind, source=source, q=q, favorite=favorite, collection_id=collection_id,
            entity_id=entity_id, sort=sort, cursor=cursor, limit=limit,
        )

        def fetch_page(page_cursor: str | None) -> Mapping[str, Any]:
            return self._client._request(
                "GET", _with_query("/assets", {**query, "cursor": page_cursor})
            )

        return _every_page(query["cursor"], fetch_page, "asset listing")

    def get(self, asset_id: str) -> dict[str, Any]:
        """One asset with a fresh private ``previewUrl``.

        An unknown asset, another account's asset and one in a library this key does not reach
        are all answered with the same 404.
        """
        return self._client._request("GET", _asset_path(asset_id))

    def update(
        self,
        asset_id: str,
        *,
        name: str | None = None,
        favorite: bool | None = None,
    ) -> dict[str, Any]:
        """Rename an asset, mark or unmark it as a favorite, or both; returns the updated asset.

        Only the fields you pass change, and the ``uri`` stays the same. Favorites belong to the
        library, so everyone in a team sees the same ones. Repeating the request is safe, so no
        Idempotency-Key is sent.
        """
        body: dict[str, Any] = {}
        if name is not None:
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            body["name"] = name
        if favorite is not None:
            if not isinstance(favorite, bool):
                raise ValueError("favorite must be True or False")
            body["favorite"] = favorite
        if not body:
            # The server would answer an empty body with a misleading "invalid name".
            raise ValueError("update needs a name, a favorite flag, or both")
        return self._client._request("PATCH", _asset_path(asset_id), body=body)

    def delete(self, asset_id: str) -> dict[str, Any]:
        """Delete an asset; returns ``{"id": ..., "deleted": True}``.

        It disappears at once and its ``uri`` stops working in new requests. Deleting an unknown
        or already deleted asset returns the same result, so repeating it is safe and no
        Idempotency-Key is sent.
        """
        return self._client._request("DELETE", _asset_path(asset_id))

    def usage(self) -> dict[str, Any]:
        """The storage plan, what is stored, the limits and how many assets were saved today.

        ``scope`` says which library the key reaches (``workspaceId``, ``personal``), the role it
        acts with and whether it may write (``canWrite``) or top up (``canTopUp``). When
        ``canTopUp`` is false (a team key whose creator is not the owner), ``lifetimeTopupUsd``
        and ``topupToNextTierUsd`` are omitted, so read them with ``.get``.
        """
        return self._client._request("GET", "/assets/usage")

    def upload_file(
        self,
        path: str,
        *,
        name: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local file and save it to the library in one call; returns the saved asset.

        The upload goes through ``client.upload_file``, so its type and size limits apply.
        ``name`` defaults to the file's base name, cut to 80 characters. Every call uploads again,
        so there is no idempotency key to pass: the save step uses a fresh one for its own retries.
        Nothing is saved when the upload fails.
        """
        uploaded = self._client.upload_file(path, content_type=content_type)
        if name is None:
            base = os.path.basename(os.fspath(path)).strip()
            name = base[:ASSET_NAME_MAX_CHARS] or None
        return self.create(source="upload", file_id=str(uploaded["fileId"]), name=name)


class SpicyClient:
    assets: SpicyAssets
    """The asset library the key reaches: personal, or the team's shared library for a team key.

    Save files once and reference them as ``spicy://a/<id>`` in any media field of model inputs
    sent with keys of the same workspace, until they are deleted.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = API_BASE_URL,
        transport: Transport = _urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        random_value: Callable[[], float] = random.random,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        upload_timeout_seconds: float = UPLOAD_TIMEOUT_SECONDS,
        wait_timeout_seconds: float = WAIT_TIMEOUT_SECONDS,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key or os.environ.get("SPICY_API_KEY", "")
        if not self.api_key:
            raise ValueError("SPICY_API_KEY is required")
        self.base_url = _normalise_base_url(base_url)
        self.transport = transport
        self.sleep = sleep
        self.monotonic = monotonic
        self.random_value = random_value
        self.request_timeout_seconds = request_timeout_seconds
        self.upload_timeout_seconds = upload_timeout_seconds
        self.wait_timeout_seconds = wait_timeout_seconds
        self.max_retries = max_retries
        self.assets = SpicyAssets(self)

    # ── Models ────────────────────────────────────────────────────────────

    def list_models(
        self,
        *,
        modality: str | None = None,
        provider: str | None = None,
        task: str | None = None,
        search: str | None = None,
        include_schema: bool | None = None,
        include_examples: bool | None = None,
    ) -> dict[str, Any]:
        values: dict[str, str] = {}
        for key, value in {
            "modality": modality,
            "provider": provider,
            "task": task,
            "search": search,
            "includeSchema": None if include_schema is None else str(int(include_schema)),
            "includeExamples": None if include_examples is None else str(int(include_examples)),
        }.items():
            if value is not None:
                values[key] = value
        suffix = f"?{urllib.parse.urlencode(values)}" if values else ""
        return self._request("GET", f"/models{suffix}")

    def get_model(self, model: str) -> dict[str, Any]:
        if not model:
            raise ValueError("model is required")
        encoded = urllib.parse.quote(model, safe="")
        return self._request("GET", f"/models/{encoded}")

    # ── Tasks ─────────────────────────────────────────────────────────────

    def quote_task(
        self,
        *,
        model: str,
        input_data: Mapping[str, Any],
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        """Price a task without creating it or reserving any funds.

        Returns quoteId, estimatedCost, maxCharge and expiresAt. The quote is
        bound to this account, API key and request and lives five minutes; pass
        its quoteId together with estimatedCost as expectedCost to create_task
        to be rejected rather than charged if the price moved in between.

        A quote reserves nothing, so it never guarantees later availability.
        """
        body: dict[str, Any] = {"model": model, "input": dict(input_data)}
        if callback_url is not None:
            body["callBackUrl"] = callback_url
        return self._request("POST", "/jobs/quote", body=body)

    def create_task(
        self,
        *,
        model: str,
        input_data: Mapping[str, Any],
        idempotency_key: str,
        callback_url: str | None = None,
        quote_id: str | None = None,
        expected_cost: str | None = None,
        wait_seconds: int | None = None,
        retention_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Submit one asynchronous generation task.

        HTTP 202 means accepted, not finished. The estimated cost is held, not
        charged; the final charge is capped at the hold.

        Keep one Idempotency-Key per logical submission and reuse it for every
        resend of that submission, including after a timeout or a dropped
        connection. A lost response does not prove the task was not created,
        and a fresh key turns an unknown outcome into a second paid task. The
        same key with different request semantics returns 409 instead.

        Pass quote_id and expected_cost from quote_task to make a price change
        fail with business code 40901 before any funds are reserved.

        There is deliberately no content-mode flag. What a model will produce is
        decided by the model you pick, not by a per-request declaration, and the
        request schema has no such field. Older clients may still send one; the
        service accepts and ignores it.

        wait_seconds holds the connection open for up to that many seconds so the
        first poll is unnecessary. **It does not guarantee a finished task**: if
        the budget runs out you get the ordinary accepted response and poll from
        there, exactly as if you had not asked. Branch on the state, never on the
        fact that you passed the parameter::

            task = client.create_task(..., wait_seconds=30)
            if not is_terminal(task):
                task = client.wait_for_terminal(task["taskId"])

        The server clamps anything above 60 and ignores anything that is not a
        positive integer, so nothing is clamped here; only a negative value is
        refused, since that is a caller mistake rather than a platform limit that
        could be relaxed later. Disconnecting stops the wait and nothing else —
        the task keeps running and is billed as usual.

        retention_seconds is sent as ``X-Spicy-Retention`` and shortens how long
        this one task's outputs and stored request text are kept. It only ever
        shortens: the effective value is the smallest of this, the account
        setting and the platform maximum, and it comes back in the task record's
        ``retention``, which is where to read it. ``0`` removes the outputs as
        soon as the task reaches a terminal state, so download them from the
        terminal record straight away. Only a negative or non-integer value is
        refused here (the server would silently ignore a non-integer and keep
        the account setting); the ceiling is the platform's to clamp.
        ``retry_task`` takes none of its own: a retry inherits its source task's
        retention.
        """
        if not idempotency_key.strip():
            raise ValueError("Idempotency-Key is required")
        request_headers = {"Idempotency-Key": idempotency_key}
        if retention_seconds is not None:
            # The server ignores anything that is not a decimal integer and keeps the account
            # setting, without saying so: 3600.0 would go out as "3600.0", and True (an int
            # subclass) as "True". Refuse both here, where the caller can still see why.
            if isinstance(retention_seconds, bool) or not isinstance(retention_seconds, int):
                raise ValueError("retention_seconds must be a whole number of seconds")
            if retention_seconds < 0:
                raise ValueError("retention_seconds must not be negative")
            request_headers["X-Spicy-Retention"] = str(retention_seconds)
        body: dict[str, Any] = {"model": model, "input": dict(input_data)}
        if callback_url is not None:
            body["callBackUrl"] = callback_url
        if quote_id is not None:
            body["quoteId"] = quote_id
        if expected_cost is not None:
            body["expectedCost"] = expected_cost
        path = "/jobs/createTask"
        timeout_seconds = None
        if wait_seconds is not None:
            if wait_seconds < 0:
                raise ValueError("wait_seconds must not be negative")
            path = f"{path}?{urllib.parse.urlencode({'wait': wait_seconds})}"
            # The local request timeout has to accommodate the server-side wait, or this call is
            # cut off locally while the server is still waiting - the feature looks like it "does
            # not work" when the real cause is two timeouts colliding. Ten seconds of headroom
            # covers task creation itself plus the round trip.
            timeout_seconds = max(self.request_timeout_seconds, wait_seconds + 10)
        return self._request(
            "POST",
            path,
            body=body,
            headers=request_headers,
            timeout_seconds=timeout_seconds,
        )

    def get_task(
        self,
        task_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not task_id:
            raise ValueError("task_id is required")
        query = urllib.parse.urlencode({"taskId": task_id})
        return self._request(
            "GET",
            f"/jobs/recordInfo?{query}",
            timeout_seconds=timeout_seconds,
        )

    def list_tasks(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        state: str | None = None,
        model: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List this API key's tasks, newest first; metadata only.

        Dates are a UTC half-open interval [from, to) of at most 92 days.
        Scope is this API key: other keys on the account and console
        generations are not included.
        """
        # Reject only non-positive values, which are unambiguously a caller error. The upper
        # bound is deliberately not enforced locally: the day the platform relaxes it from 100 to
        # 200, this would reject a value that already works, and that failure emits no signal at
        # all - the user sees the SDK refuse while the server plainly accepts it. The same
        # reasoning applies to the X-Spicy-Retention limit, and both follow it.
        if limit is not None and limit < 1:
            raise ValueError("limit must be a positive integer")
        values: dict[str, str] = {}
        for key, value in {
            "from": from_date,
            "to": to_date,
            "state": state,
            "model": model,
            "limit": None if limit is None else str(limit),
            "cursor": cursor,
        }.items():
            if value is None:
                continue
            if value == "":
                # An empty string is not "unset": the caller computed a filter and it came out
                # empty.
                #
                # Dropping it silently hands them the entire unfiltered list with nothing to say
                # the filter did not apply; sending it as-is earns a 400 from the server that does
                # not name the parameter. Neither is good, so reject it here by name.
                raise ValueError(
                    f"{key} must not be an empty string; omit it to leave that filter off"
                )
            values[key] = value
        suffix = f"?{urllib.parse.urlencode(values)}" if values else ""
        return self._request("GET", f"/jobs{suffix}")

    def iter_tasks(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        state: str | None = None,
        model: str | None = None,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Walk every page of list_tasks, yielding one task at a time.

        Every filter is repeated unchanged on each page, as the contract
        requires; changing one mid-walk makes the cursor meaningless. Pages
        read live state rather than a frozen snapshot, so a task may move
        between states while you paginate.
        """
        cursor: str | None = None
        while True:
            page = self.list_tasks(
                from_date=from_date,
                to_date=to_date,
                state=state,
                model=model,
                limit=limit,
                cursor=cursor,
            )
            yield from page.get("items") or []
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                return

    def retry_task(self, task_id: str, idempotency_key: str) -> dict[str, Any]:
        if not task_id:
            raise ValueError("task_id is required")
        if not idempotency_key.strip():
            raise ValueError("Idempotency-Key is required")
        return self._request(
            "POST",
            "/jobs/retry",
            body={"taskId": task_id},
            headers={"Idempotency-Key": idempotency_key},
        )

    def purge_task(self, task_id: str) -> dict[str, Any]:
        """Destroy a finished task's stored content: media, result and prompt.

        Billing evidence is never touched. The ledger, the charged amount, the
        model, the state, the timestamps and the request_id all remain, and the
        response repeats that as billingRetained.

        Only a terminal task can be destroyed; queued or running returns 400.
        No Idempotency-Key is sent: this endpoint does not read one, the taskId
        is the idempotency key, and a repeat returns the original purgedAt. So
        a timed-out call is safe to send again.
        """
        if not task_id:
            raise ValueError("task_id is required")
        return self._request("POST", "/jobs/purge", body={"taskId": task_id})

    def wait_for_terminal(
        self,
        task_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        total_timeout = self.wait_timeout_seconds if timeout_seconds is None else timeout_seconds
        deadline = self.monotonic() + total_timeout
        interval = 2.0

        while self.monotonic() < deadline:
            remaining = deadline - self.monotonic()
            # When the remaining budget cannot fund a meaningful request, break out and raise the
            # timeout below - the one that carries the task_id.
            #
            # Without this floor the final lap would issue a request with a near-zero timeout, and
            # that request is all but guaranteed to time out - raising a plain "request timed out"
            # with no task_id. The caller loses the task id at the exact moment it matters most:
            # the task is still running and still being billed, while all they hold is "the request
            # timed out". What is lost is not one poll but the only thread back to that task.
            #
            # The floor is one second: a round trip shorter than that would not return anything
            # useful anyway.
            if remaining < 1.0:
                break
            try:
                task = self.get_task(
                    task_id,
                    timeout_seconds=min(self.request_timeout_seconds, remaining),
                )
            except SpicyTimeoutError:
                # One polling attempt timing out is not this wait failing - while budget remains,
                # keep polling.
                #
                # Letting it propagate gets two things wrong at once. It loses the task_id: this
                # timeout comes from _request, which does not know which task it is polling for.
                # And it promotes one piece of network turbulence into the end of the whole wait -
                # a default 600-second budget abandoned because the first attempt stalled for 30,
                # while the task keeps running and keeps being billed.
                #
                # When the budget genuinely runs out, the floor at the top of the loop breaks out
                # and raises the timeout below, which does carry the task_id. The TypeScript client
                # needs none of this: its whole wait shares one abort signal, and that signal's
                # reason carries the taskId. There is no signal here, so it has to be caught
                # explicitly.
                pass
            else:
                state = task.get("state")
                if state in TERMINAL_STATES and not self._has_pending_assets(task):
                    return task
                if state not in TERMINAL_STATES and state not in ACTIVE_STATES:
                    raise SpicyApiError(f"unknown task state: {state!r}", status=200, code=200)

            delay = min(self._jitter(interval), max(0.0, deadline - self.monotonic()))
            if delay > 0:
                self.sleep(delay)
            interval = min(interval * 1.5, 15.0)

        raise SpicyTimeoutError(
            f"task {task_id} exceeded the local {total_timeout}s polling deadline; "
            "its remote state is unknown",
            task_id=task_id,
        )

    @staticmethod
    def _has_pending_assets(task: Mapping[str, Any]) -> bool:
        """True while a succeeded task still has assets without a URL.

        A task can reach succeeded before every output object has landed. Those
        assets carry pending: true and no url, so returning here would hand the
        caller a result it cannot download.
        """
        if task.get("state") != "succeeded":
            return False
        return any(
            asset.get("pending") and not asset.get("unavailable")
            for asset in output_assets(task)
        )

    # ── Media ─────────────────────────────────────────────────────────────

    def create_upload_url(self, *, content_type: str, byte_count: int) -> dict[str, Any]:
        """Request a presigned upload ticket for an exact number of bytes.

        The ticket is not retried on a transient failure: each one is a signed
        write authorization against a tight per-account fuse, and a second
        ticket does not make the first one usable.
        """
        if content_type not in UPLOAD_CONTENT_TYPES:
            raise ValueError(
                f"content_type must be one of {sorted(UPLOAD_CONTENT_TYPES)}, got {content_type!r}"
            )
        limit = max_upload_bytes(content_type)
        if byte_count < 1 or byte_count > limit:
            raise ValueError(
                f"{content_type} uploads must be between 1 and {limit} bytes, got {byte_count}"
            )
        return self._request(
            "POST",
            "/common/upload-url",
            body={"contentType": content_type, "bytes": byte_count},
            retryable=False,
        )

    def commit_file(self, file_id: str) -> dict[str, Any]:
        """Verify and freeze an uploaded object; returns the spicy:// URI.

        Only data["uri"] may be used in task input — never the upload URL, the
        temporary object key, or the bare file ID. Repeating a successful
        commit is idempotent, so this call is safe to retry.
        """
        if not file_id:
            raise ValueError("file_id is required")
        encoded = urllib.parse.quote(file_id, safe="")
        return self._request("POST", f"/files/{encoded}/commit")

    def upload_bytes(self, data: bytes, *, content_type: str) -> dict[str, Any]:
        """Ticket, PUT and commit in one call; returns the commit record.

        Put the returned ``uri`` into the model input field whose x-ui.widget
        is "upload" (or inside the list for "multi-upload"), or into the
        ``url`` of an ``image_url`` / ``video_url`` / ``audio_url`` part of a
        chat message sent through an OpenAI-compatible library. LoRA weights
        (``application/octet-stream``, a ``.safetensors`` file) go into
        ``loras[].path``.

        The URI lasts one day; to keep a file longer, save it to the asset
        library with ``client.assets.create(source="upload", file_id=...)``.
        """
        ticket = self.create_upload_url(content_type=content_type, byte_count=len(data))
        max_bytes = ticket.get("maxBytes")
        if isinstance(max_bytes, int) and len(data) > max_bytes:
            raise SpicyUploadError(
                f"file of {len(data)} bytes exceeds the ticket limit of {max_bytes} bytes",
                status=413,
            )
        self._put_bytes(
            str(ticket["uploadUrl"]),
            # Every ticket header goes out exactly as received. Content-Type and
            # Content-Length are both signed, so altering or dropping one makes
            # storage answer 403 with an error that does not come from us.
            dict(ticket.get("headers") or {}),
            data,
            method=str(ticket.get("method") or "PUT"),
        )
        return self.commit_file(str(ticket["fileId"]))

    def upload_file(self, path: str, *, content_type: str | None = None) -> dict[str, Any]:
        """Upload a local file. The content type is inferred from its name.

        The type and the size are settled before the file is read: rejecting an
        unsupported or oversized file should not cost 90 MiB of memory first.
        """
        resolved = content_type or content_type_for_path(path)
        limit = max_upload_bytes(resolved)
        size = os.path.getsize(path)
        if size < 1 or size > limit:
            raise ValueError(
                f"{resolved} uploads must be between 1 and {limit} bytes, got {size}"
            )
        with open(path, "rb") as handle:
            data = handle.read()
        return self.upload_bytes(data, content_type=resolved)

    def create_download_url(self, task_id: str, key: str | None = None) -> dict[str, Any]:
        """Mint a fresh short-lived link to one of a task's outputs.

        Ready results already carry a usable output.assets[].url, so this is
        only needed once that link has expired. Polling the task again works
        just as well. Fetch the returned URL without any Authorization header:
        it is itself the credential.
        """
        if not task_id:
            raise ValueError("task_id is required")
        body: dict[str, Any] = {"taskId": task_id}
        if key is not None:
            body["key"] = key
        return self._request("POST", "/common/download-url", body=body, retryable=False)

    # ── Account ───────────────────────────────────────────────────────────

    def get_balance(self) -> dict[str, Any]:
        """Net available, held and total balance in USD decimal strings."""
        return self._request("GET", "/chat/credit")

    def get_usage(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Settled spend and call counts for this API key over [from, to).

        For reconciliation, not for progress: this endpoint has its own
        account-wide budget of 30 requests per minute shared by every key, and
        that limiter fails closed, so polling it can lock every key on the
        account out of its own reporting. Follow a task with get_task instead.

        Late settlement can change a previous day's total, so a figure read
        today is not final.
        """
        values: dict[str, str] = {}
        if from_date is not None:
            values["from"] = from_date
        if to_date is not None:
            values["to"] = to_date
        suffix = f"?{urllib.parse.urlencode(values)}" if values else ""
        return self._request("GET", f"/usage{suffix}")

    # ── Transport ─────────────────────────────────────────────────────────

    def _put_bytes(
        self,
        url: str,
        headers: Mapping[str, str],
        data: bytes,
        *,
        method: str = "PUT",
    ) -> None:
        """Send the bytes to object storage. Not a SpicyAPI call.

        No Authorization header: the presigned URL carries its own credential
        and some storage implementations reject a request that has both. No
        envelope either — a storage response body is XML, not our JSON.

        One attempt only, on the upload timeout rather than the API timeout.
        """
        try:
            status, _, _ = self.transport(method, url, headers, data, self.upload_timeout_seconds)
        except TimeoutError as error:
            raise SpicyTimeoutError(
                f"upload exceeded the local {self.upload_timeout_seconds}s timeout"
            ) from error
        except (urllib.error.URLError, OSError) as error:
            raise SpicyUploadError(f"presigned upload failed: {error}") from error
        if not 200 <= status < 300:
            raise SpicyUploadError(
                f"presigned upload failed with HTTP {status}; the ticket headers must be sent "
                "unchanged and the body must be exactly the declared number of bytes",
                status=status,
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        retryable: bool = True,
    ) -> Any:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": _user_agent(),
            "Authorization": f"Bearer {self.api_key}",
            **({"Content-Type": "application/json"} if body is not None else {}),
            **dict(headers or {}),
        }
        encoded_body = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        timeout = self.request_timeout_seconds if timeout_seconds is None else timeout_seconds
        # Per-attempt timeout: an explicitly supplied budget wins, and the client's default
        # request timeout applies only when none was given.
        #
        # Hard-coding self.request_timeout_seconds here would make "create a task with wait"
        # silently ineffective - the server is still waiting while the local side cuts the call off
        # after 30 seconds, so the feature looks like it does nothing when the real cause is two
        # timeouts colliding.
        per_attempt = self.request_timeout_seconds if timeout_seconds is None else timeout
        request_deadline = self.monotonic() + timeout

        for attempt in range(self.max_retries + 1):
            remaining = request_deadline - self.monotonic()
            if remaining <= 0:
                raise SpicyTimeoutError(
                    f"request exceeded the local {timeout}s timeout"
                )
            try:
                status, response_headers, raw = self.transport(
                    method,
                    f"{self.base_url}{path}",
                    request_headers,
                    encoded_body,
                    min(per_attempt, remaining),
                )
            except TimeoutError as error:
                if retryable and attempt < self.max_retries:
                    self.sleep(min(
                        self._retry_delay(attempt),
                        max(0.0, request_deadline - self.monotonic()),
                    ))
                    continue
                raise SpicyTimeoutError(
                    f"request exceeded the local {timeout}s timeout"
                ) from error
            except (urllib.error.URLError, OSError) as error:
                if retryable and attempt < self.max_retries:
                    self.sleep(min(
                        self._retry_delay(attempt),
                        max(0.0, request_deadline - self.monotonic()),
                    ))
                    continue
                raise SpicyApiError(f"network request failed: {error}") from error

            retry_after = _parse_retry_after(_header(response_headers, "Retry-After"))
            try:
                envelope = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                if retryable and attempt < self.max_retries and status in RETRYABLE_HTTP:
                    self.sleep(min(
                        self._retry_delay(attempt, _header(response_headers, "Retry-After")),
                        max(0.0, request_deadline - self.monotonic()),
                    ))
                    continue
                raise SpicyApiError(
                    "response was not valid JSON", status=status
                ) from error

            code = envelope.get("code") if isinstance(envelope, dict) else None
            message = envelope.get("msg") if isinstance(envelope, dict) else None
            request_id = envelope.get("request_id", "") if isinstance(envelope, dict) else ""
            failed = not 200 <= status < 300 or code != 200
            # 40004 and 40901 never enter this set: the identical request gets
            # the identical answer, and only the caller can change a parameter
            # or accept a new price.
            resend_helps = code not in NON_RETRYABLE_CODES and (
                status in RETRYABLE_HTTP or code in RETRYABLE_CODES
            )
            # When the server asks for a wait longer than this call has left, hand its error back
            # rather than sleeping out the budget. Otherwise the caller gets "request exceeded the
            # local timeout" in place of what the server actually said - and for the asset
            # library's daily limit (429 with Retry-After until 00:00 UTC) that would bury the
            # reason, the Retry-After and the request_id under a timeout that never happened.
            outlasts_budget = (
                retry_after is not None and retry_after > request_deadline - self.monotonic()
            )
            if (
                failed
                and retryable
                and resend_helps
                and not outlasts_budget
                and attempt < self.max_retries
            ):
                self.sleep(min(
                    self._retry_delay(attempt, _header(response_headers, "Retry-After")),
                    max(0.0, request_deadline - self.monotonic()),
                ))
                continue
            if failed:
                raise SpicyApiError(
                    message if isinstance(message, str) else f"request failed with HTTP {status}",
                    status=status,
                    code=code if isinstance(code, int) else None,
                    request_id=request_id if isinstance(request_id, str) else "",
                    retry_after_seconds=retry_after,
                    reason=_failure_reason(
                        envelope.get("data") if isinstance(envelope, dict) else None
                    ),
                )
            if "data" not in envelope:
                raise SpicyApiError(
                    "successful envelope omitted data",
                    status=status,
                    code=code,
                    request_id=request_id,
                )
            return envelope["data"]

        raise AssertionError("retry loop exited unexpectedly")

    def _jitter(self, seconds: float) -> float:
        return seconds * (0.8 + self.random_value() * 0.4)

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        exponential = min(0.5 * (2**attempt), 8.0)
        server_delay = _parse_retry_after(retry_after) or 0.0
        return max(self._jitter(exponential), server_delay)
