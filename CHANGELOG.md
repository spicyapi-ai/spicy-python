# Changelog

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
