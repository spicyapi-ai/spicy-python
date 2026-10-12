<div align="center">

# spicyapi

**Official Python SDK for [SpicyAPI](https://spicyapi.ai)** — image, video, audio and text models behind one API.

[Get a key](https://spicyapi.ai) · [Models](https://spicyapi.ai/models) · [Docs](https://docs.spicyapi.ai) · [Status](https://status.spicyapi.ai)

</div>

---

One endpoint in front of the whole SpicyAPI catalogue — image, video, audio (speech, music and
transcription) and text models — billed in USD per request rather than in credits. Media generation
is asynchronous and quotable before you spend; text models speak the OpenAI, Anthropic and Gemini
wire formats.

```bash
pip install spicyapi
```

Requires Python 3.11+. **No runtime dependencies**: this package goes into your dependency tree, and
every constraint it adds is one more chance of a conflict.

## Generate something

```python
import os, uuid
from spicyapi import SpicyClient, output_assets

client = SpicyClient()  # reads SPICY_API_KEY from the environment

model = client.get_model("MODEL_ID_FROM_CATALOG")     # copy a real id from list_models()
quote = client.quote_task(model=model["model"], input_data={"prompt": "a lantern in fog"})
print(quote["estimatedCost"], quote["maxCharge"])      # decide before you spend

task = client.create_task(
    model=model["model"],
    input_data={"prompt": "a lantern in fog"},
    idempotency_key=str(uuid.uuid4()),
    quote_id=quote["quoteId"],
    expected_cost=quote["estimatedCost"],
)
final = client.wait_for_terminal(task["taskId"])
for asset in output_assets(final):                     # module-level helper, not a method
    print(asset["url"])
```

Build the `input` from that model's own `inputSchema` — every model has different fields, and
`list_models(include_schema=True)` returns them.

## Start from a local file

Image-to-video, face swap, image editing, lip sync and transcription all need your material on our
side first. Upload returns a `spicy://` URI; that is what goes into `input`. Images (JPEG, PNG,
WebP, GIF) go up to 10 MiB; video (MP4, WebM), audio (MP3, WAV) and reference documents (PDF,
Word, Excel, PowerPoint, Keynote, Pages, Numbers, plain text, Markdown) up to 90 MiB; LoRA weights
(a `.safetensors` file, sent as `application/octet-stream`, for `loras[].path`) up to 2 GiB. For a
file that large, raise `upload_timeout_seconds` on the client above its ten-minute default.

```python
uploaded = client.upload_file("/path/to/reference.png")
task = client.create_task(
    model="MODEL_ID_FROM_CATALOG",
    input_data={"image": uploaded["uri"], "prompt": "slow dolly in"},
    idempotency_key=str(uuid.uuid4()),
)
```

An upload's URI lasts one day. To keep a file for later requests, save it to the
[asset library](#asset-library).

## Asset library

Files you use again and again — a character portrait, a style reference, a voice sample — belong in
your asset library: each saved file gets a stable `uri` of the form `spicy://a/<id>` that works in
any media field of model inputs sent with keys of the same workspace, until it is deleted. The
library never hands out a public or permanent link.

The key decides the library. A key created in your personal workspace reaches your personal library;
a key created in a team workspace, by any member, reaches that team's shared library and never
anyone's personal one. With a team key, what may change follows the current role of the member who
created it: owner and admin can save, rename, favorite and delete anything; developer can save, and
rename, favorite or delete what they saved; billing and readonly can only read and use assets.

```python
from spicyapi import SpicyClient

client = SpicyClient()

# Save the first output of one of your successful tasks (with a team key, one of the team's).
# source="url" with url=..., and source="upload" with file_id=..., work the same way. The required
# Idempotency-Key is added for you.
hero = client.assets.create(source="task_output", task_id="job_...", index=0, name="Hero portrait")

# Or upload a local file and save it in one call; the name defaults to the file name.
voice = client.assets.upload_file("./voice-sample.wav")

# Later, in any request: the uri goes where the model's schema expects a file.
input_data = {"prompt": "Slow push-in, light rain", "image_url": hero["uri"]}

# Every saved image, newest first, across all pages.
for asset in client.assets.iter_all(kind="image", q="portrait"):
    print(asset["uri"], asset["name"], asset["bytes"])
```

| Method | Returns |
| --- | --- |
| `assets.create(source=..., task_id=, key=, index=, file_id=, url=, name=, idempotency_key=)` | The saved asset: `id`, `uri`, `name`, `kind`, `bytes`, `favorite`, `model`, `previewUrl`, … |
| `assets.upload_file(path, name=, content_type=)` | Uploads with `upload_file`, then saves; the saved asset |
| `assets.list(kind=, source=, q=, favorite=, collection_id=, entity_id=, sort=, cursor=, limit=)` | One page: `items`, `nextCursor` (absent on the last page) |
| `assets.iter_all(...)` | An iterator over every matching asset, with the same filters as `list` |
| `assets.get(asset_id)` | One asset with a fresh `previewUrl` |
| `assets.update(asset_id, name=, favorite=)` | The updated asset; only the fields you pass change, the `uri` stays the same |
| `assets.delete(asset_id)` | `{"id": ..., "deleted": True}`; the `uri` stops working at once |
| `assets.usage()` | `scope` (which library, role, `canWrite`, `canTopUp`), plan, usage |
| `assets.characters.list(asset_id=, cursor=, limit=)` / `iter_all(...)` | Characters, newest first: `id`, `name`, `note`, `cover`, `assets` (the images in order), `count` |
| `assets.characters.get(character_id)` | One character with its images in order |

- `asset_id` may be the bare `ast_…` id or the `spicy://a/…` reference. An empty id, a blank filter
  or an unknown `sort` raises `ValueError` before anything is sent.
- `source` takes one value or a list of them (`task_output`, `upload`, `url`). `favorite=True` lists
  only favorites and `favorite=False` only the rest; leave it as `None` for both.
- `sort` is `recent` (the default, newest first), `oldest`, `name` or `size`. A `nextCursor` only
  works with the `sort` it came from; sending it with another one fails with `400`, which is raised
  as-is and not retried. `iter_all` keeps your filters and `sort` on every page.
- Saving the same task output, upload or URL again returns the asset you already have. Pass your own
  `idempotency_key` to replay a save safely after an uncertain response, for example across a
  restart.
- `previewUrl` is a private preview signed for at most 20 minutes. Show it; store `uri`.
- Storage unlocks with the owner's lifetime top-ups, and the owner's personal and team libraries
  share one allowance. `assets.usage()` reports the whole allowance; with a team key whose creator
  is not the owner, `scope.canTopUp` is false and `lifetimeTopupUsd` and `topupToNextTierUsd` are
  omitted, so read both with `.get`.
- A refused call raises `SpicyApiError` with a `reason`: `asset_library_locked`,
  `asset_library_full` and `asset_file_too_large` (400, plan limits — read `assets.usage()` for the
  numbers), `asset_daily_limit` (429, resets at 00:00 UTC, with `retry_after_seconds`),
  `asset_storage_full` (503, temporary), `asset_permission_denied` (403, the team role does not
  allow this change), `asset_workspace_forbidden` (403, this key cannot reach that library, for
  example because its creator left the team) and `asset_entity_name_taken` (409, a character with
  that name already exists). The set, which matches the contract, is exported as
  `ASSET_ERROR_REASONS`; `reason` carries any other value the server sends unchanged, so keep a
  default branch.

```python
from spicyapi import SpicyApiError

try:
    client.assets.create(source="url", url="https://example.com/still.png")
except SpicyApiError as error:
    if error.reason != "asset_library_full":
        raise
    usage = client.assets.usage()
    next_tier = (usage.get("nextTier") or {}).get("name", "none")
    print(f"Full on {usage['tier']['name']}; next plan: {next_tier}")
```

### Favorites, folders and characters

Every asset has a `favorite` flag; in a team library everyone sees the same favorites. Folders
(collections in the API) and characters are created and arranged on the Assets page of the web app;
the API reads them. Each asset also carries `model` and `modelName`, the model that generated it
(empty strings for uploads and links).

```python
# Mark a favorite, then list only favorites in one folder, A to Z.
client.assets.update(hero["uri"], favorite=True)
page = client.assets.list(favorite=True, collection_id="col_...", sort="name")

# A character is a named, ordered set of images. Put its images, in order, into the model's
# reference image field (get_model shows which field takes a list of images).
mia = client.assets.characters.get("ety_...")
references = [image["uri"] for image in mia["assets"]]  # ["spicy://a/ast_...", ...]
```

An unknown folder, and an unknown character, both fail with `404`. Characters used to be called
personas: existing `spicy://p/<id>` references keep resolving to the same images, in your personal
library only; new code should use the `spicy://a/` references a character lists.

See the asset library guide:
[organizing](https://docs.spicyapi.ai/docs/media#asset-library-organize) and
[characters](https://docs.spicyapi.ai/docs/media#asset-library-characters).

## Results that are not files

Most models return files in `output.assets`. Chat and transcription answer in text instead, so read
results through the helpers rather than indexing `output` directly — a successful task may simply
not have the key you reached for.

```python
from spicyapi import output_text, output_transcript

final = client.wait_for_terminal(task["taskId"])
print(output_text(final))                         # the plain transcript, or None
transcript = output_transcript(final)             # timed words and detected language, when returned
if transcript:
    for word in transcript.get("words", []):
        print(word["start"], word["end"], word["text"])
```

To keep a task's outputs and prompt for less time than your account setting, pass
`retention_seconds` to `create_task`. It can only shorten retention; the value that applied comes back
in the task's `retention`.

## Webhooks

`verify_webhook` is a module-level function, so a request handler can use it without building a
client. It compares in constant time and checks the timestamp only after the signature is valid.

```python
from spicyapi import verify_webhook

delivery = verify_webhook(
    raw_body=request.body,                      # the exact bytes, before any parsing
    signature=request.headers["X-Webhook-Signature"],
    timestamp=request.headers["X-Webhook-Timestamp"],
    payload_version=request.headers["X-Webhook-Payload-Version"],
    secret=os.environ["SPICY_WEBHOOK_SECRET"],
)
```

Verify the raw bytes. Re-serialising the parsed JSON changes them, and the signature will never match.

## Two things that will save you money

**Keep one idempotency key per submission.** Reuse it for every resend of that submission, including
after a timeout or a dropped connection. A lost response does not prove the task was not created — a
fresh key turns an unknown outcome into a second paid task.

**A task that succeeds is charged, even if the result disappoints.** Quote first when the price
matters; `quote_task` reserves nothing.

## What this package does not do

**Text models.** They speak the OpenAI, Anthropic and Google Gemini wire formats, so the official
libraries for those already work — point them at `https://api.spicyapi.ai/v1` with the same key.
Wrapping them here would add nothing.

Some chat models also read images, video and audio. An `audio_url` part accepts only an uploaded
file, and video is reliable only as one (a public video URL works only if the model's provider can
fetch it), so upload with this package and send the URI through the OpenAI library:

```python
from openai import OpenAI

clip = client.upload_file("/path/to/clip.mp4")
llm = OpenAI(base_url="https://api.spicyapi.ai/v1", api_key=os.environ["SPICY_API_KEY"])
answer = llm.chat.completions.create(
    model="google/gemini-3.7-flash/chat",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "What happens in this clip?"},
        {"type": "video_url", "video_url": {"url": clip["uri"]}},
    ]}],
)
print(answer.choices[0].message.content)
```

Which parts a model takes (`image_url`, `video_url`, `audio_url`, `input_audio`) is listed in its
`inputSchema`; a part it does not list is refused with `400` and nothing is charged. The uploaded
video and audio in one chat request may total 14 MB, well under the 90 MiB upload ceiling.

**Browser, mobile and desktop apps.** Never ship this key inside an application: a key compiled into
a client is a public key. Call from your server, or put
`@spicyapi/proxy` in front.

## Errors

Every failure raises `SpicyApiError` with `status`, `code`, `request_id`, `retry_after_seconds` and,
when the server sends one, `reason` (see [Asset library](#asset-library)). Branch on `code` or
`reason`, never on the message text — messages are translated, codes are not.

`503` is shared by three different business codes, so reading the HTTP status alone is not enough:

| code | meaning | what to do |
| --- | --- | --- |
| `40003` | uploaded bytes do not match their ticket | upload again |
| `40004` | no deployment serves that parameter combination | change the parameter named in the message |
| `40310` | the account has not verified its email address | open the verification link, then call again |
| `40901` | the price moved before the task was created | quote again, keep the same idempotency key |
| `503` | a dependency is briefly unavailable | back off by `Retry-After` |
| `50301` | the model has no usable deployment or price right now | do not hammer; refresh the catalogue |
| `50302` | a synchronous generation failed upstream and was refunded | retry with a **new** idempotency key |

`err.recovery` carries the same guidance at runtime.

Retryable failures are retried with backoff inside one request's timeout. When the server's
`Retry-After` is longer than what that timeout has left, the error is raised at once, with its
`retry_after_seconds`, instead of being slept through into a local timeout.

## Links

- [Documentation](https://docs.spicyapi.ai)
- [API reference](https://docs.spicyapi.ai/docs/api-reference)
- [Source](https://github.com/spicyapi-ai/spicy-python)

---

<div align="center">
<sub>

Also available in [TypeScript](https://github.com/spicyapi-ai/spicy-sdk) · **Python** · [Go](https://github.com/spicyapi-ai/spicy-go) · [PHP](https://github.com/spicyapi-ai/spicy-php) · [Java](https://github.com/spicyapi-ai/spicy-java)

</sub>
</div>
