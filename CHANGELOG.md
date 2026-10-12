# Changelog

## 0.3.0

### Added

- **The asset library, as `client.assets`.** Saved files are kept until they are deleted and are
  referenced as `spicy://a/<id>` in any media field of model inputs sent with keys of the same
  workspace; an upload's `spicy://` URI lasts one day. The key decides the library: a personal key
  reaches the personal library, a team key that team's shared library, where the creator's current
  role decides what may change.
  - `assets.create(source=..., ...)` saves a task output (`task_id` with `key` or `index`), a
    committed upload (`file_id`) or a public URL (`url`). It sends the required `Idempotency-Key`
    itself, the same one on every retry of the call; pass `idempotency_key` to replay a save.
  - `assets.list(...)` and `assets.iter_all(...)` take `kind`, `source` (one value or several),
    `q`, `favorite` (`False` is sent and lists the assets that are not favorites; `None` leaves the
    filter off), `collection_id`, `entity_id`, `sort` (`recent`, `oldest`, `name`, `size`; exported
    as `ASSET_SORTS`), `cursor` and `limit`. `iter_all` follows `nextCursor` and repeats the filters
    and sort on every page; a cursor that comes back twice is refused instead of looping.
  - `assets.get`, `assets.update` (`name`, `favorite` or both; only what you pass is sent),
    `assets.delete` and `assets.usage`, which carries `scope`.
  - `assets.upload_file(path, name=, content_type=)` uploads a local file and saves it in one call;
    the name defaults to the file name, cut to 80 characters.
  - `assets.characters.list`, `iter_all` and `get` read the library's characters (named, ordered
    sets of images) with their images in order. They are read-only through the API.
  - Asset ids are accepted as `ast_…` or as the `spicy://a/…` reference. Empty ids, blank filters,
    an unknown `sort` and a save that mixes two sources raise `ValueError` before anything is sent.
- **`SpicyApiError.reason`**, read from a failure envelope's `data.reason`. The asset library uses
  `asset_library_locked`, `asset_library_full`, `asset_file_too_large`, `asset_daily_limit`,
  `asset_storage_full`, `asset_permission_denied`, `asset_workspace_forbidden` and
  `asset_entity_name_taken`, exported as `ASSET_ERROR_REASONS` with `is_asset_error_reason`; any
  other value is passed through unchanged.
- **LoRA weight uploads.** `.safetensors` maps to `application/octet-stream`, the upload type for
  `loras[].path`, with its own 2 GiB ceiling.

### Changed

- A retryable failure whose `Retry-After` is longer than what the request's timeout has left is
  now raised at once, with its `retry_after_seconds` and `reason`. It used to be slept through and
  surface as a local timeout, which hid what the server said; the asset library's daily limit
  (429 until 00:00 UTC) is the case that made this visible. Shorter waits are retried as before.
- The pinned `contracts/openapi.yaml` matches the server's contract as of 2026-10-12, including the
  asset routes and `POST /api/v1/files/import` (which this package does not wrap).
- `scripts/check_contract.py` also guards the asset reasons, sorts and sources, and takes
  `--offline` to check against the pinned copy without downloading the published one.
  `scripts/check_readme.py` also checks `client.assets` and `client.assets.characters` examples,
  including their argument names.

## 0.2.0

### Added

- **Reference document uploads.** `create_upload_url`, `upload_bytes` and `upload_file` accept the
  twelve document types the upload contract now lists — PDF, Word (`.doc`, `.docx`), Excel (`.xls`,
  `.xlsx`), PowerPoint (`.ppt`, `.pptx`), Keynote, Pages, Numbers, plain text and Markdown — up to
  90 MiB, the same ceiling as audio and video. `upload_file` infers the type from those suffixes.
  Until now they were refused locally before the request was sent, with an error that read as if
  the platform did not take them.
- **`output_transcript(task)`**, a module-level helper beside `output_text` and `output_assets`. It
  returns the structured result of speech-to-text endpoints that provide one — timed `words`
  (with `speakerId` when the model separates speakers), the detected language, the duration and,
  for multichannel requests, `channels` — or `None` for every other task, including failed ones.
- **`create_task(retention_seconds=...)`**, sent as `X-Spicy-Retention`, to keep one task's outputs
  and stored request text for less time than the account setting. It can only shorten retention;
  the effective schedule comes back in the task's `retention`. Negative and non-integer values are
  refused locally because the server would quietly clamp or ignore them. The TypeScript, Go, PHP
  and Java clients already had this option.
- **Business code `40310`** (the account has not verified its email address) now carries recovery
  guidance in `SpicyApiError.recovery`. It arrives as HTTP 403, like a key restriction, so the status
  alone points at the wrong fix.

### Changed

- The pinned `contracts/openapi.yaml` matches the published contract as of 2026-10-05.
- The README covers audio models, document uploads, reading transcripts, and sending uploaded
  images, video and audio to chat models through the OpenAI library. It no longer quotes a model
  count, which went stale with every catalogue change.

## 0.1.1 and earlier

Released before this changelog was kept: the asynchronous task flow, the three-step upload, webhook
verification, and English-only source comments.
