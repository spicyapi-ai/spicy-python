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
Word, Excel, PowerPoint, Keynote, Pages, Numbers, plain text, Markdown) up to 90 MiB.

```python
uploaded = client.upload_file("/path/to/reference.png")
task = client.create_task(
    model="MODEL_ID_FROM_CATALOG",
    input_data={"image": uploaded["uri"], "prompt": "slow dolly in"},
    idempotency_key=str(uuid.uuid4()),
)
```

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

Every failure raises `SpicyApiError` with `status`, `code`, `request_id` and `retry_after_seconds`.
Branch on `code`, never on the message text — messages are translated, codes are not.

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
