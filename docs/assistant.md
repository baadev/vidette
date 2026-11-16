# Questions in plain words, and MCP

Write to the bot as you would to a person: "Where else was this person?", "Send the video
from the other cameras at that moment" (as a reply to an alert, so the alert is the
context), "What happened at the gate after 9?", "Today's digest". A small assistant answers
briefly, in the bot's `language` (English or Russian; it understands both), with videos
when you asked for them. The same tools are open to an
assistant on your own machine over MCP. Design:
[ADR-0017](architecture/adr/0017-subject-frames-other-cameras-and-questions.md).

Status: 🚧 built and tested against a fake Telegram API and a fake model; run against the
real local model on a test deployment's events. Answers from a model shared with other work
take one to three minutes.

## What it can do

| Tool | Answers |
|---|---|
| `find_events` | events in a period, on a camera, of a scenario, alerts only, or with one identity's face |
| `event_detail` | one event: what the judge saw, the level, faces, your answer |
| `clips_at` | what every camera recorded around a moment ([other-cameras.md](other-cameras.md)) |
| `identity_sightings` | where and when an anonymous identity was seen (by face) |
| `send_clips` | the mosaic of every camera at that moment, sent to the chat |
| `mark_event` | the same as the alert's buttons: right, false (with a reason), not sure |
| `today` | today's digest |

Anything else gets a short list of what it can do. It never describes how people look,
and two clips are «the same person» only when a face says so.

**Three questions skip the model** and answer in seconds: the other cameras at the
moment of the alert you replied to, where else the person in it was seen, and today's
digest. Everything else goes to the model with the tools, at most `max_steps` rounds.

## The model

```yaml
assistant:
  enabled: true
  backend: local            # local | cloud | none: one of understanding.vlm's endpoints
  max_calls_per_day: 300
  max_steps: 3              # tool rounds before it must answer
  timeout_s: 300            # one call; a shared server queues
```

`local` is the judge's own server (`understanding.vlm.local`), any OpenAI-compatible one
with tool calls (llama.cpp's `llama-server --jinja`, Ollama, vLLM); a server without tool
calls gets the tools as a JSON-action prompt instead. Measured with a 27B model on a
llama-server shared with other work (6 slots of 2048 tokens): one tool call took about
50 s when the server was busy. The prompt, the tool list and the results are kept under
about 2000 tokens.

`cloud` (`understanding.vlm.cloud`) is opt-in: your questions and what the tools found go
to that vendor. Results for it never carry a name (enrolled people are "one of us", identities
"#12"), and no face crop, embedding or frame is ever sent; videos go to the chat, never to
the model. `vidette validate` says so while it is on.

## In Telegram

- Only allowed chats; a message from anyone else is ignored.
- A reply to an alert, to its album or to the bot's answer about it makes that event the
  context. Without a reply, ask with a time ("what happened at 9:40?").
- After four seconds without an answer the bot says it is looking; the answer comes as a
  new message (it buzzes), videos as replies to it.
- One question at a time (the model is shared); messages older than ten minutes (sent
  while the service was down) are not answered.

## MCP

The read-only tools (`find_events`, `event_detail`, `clips_at`, `identity_sightings`,
`today`) at `POST /api/v1/mcp`: MCP's streamable HTTP transport in its simplest form (one
JSON-RPC request per POST, a JSON answer, no sessions). Behind the API's own auth: a token
with `read:events`. On the server's own address, 127.0.0.1 by default. No names in results
(an MCP client may hand them to a cloud model); videos are API links
(`/api/v1/events/{id}/clip.mp4`, `…/linked.mp4`).

```bash
# a token with read:events (log in first; the session cookie lands in cookies.txt)
curl -s -c cookies.txt -H 'content-type: application/json' \
  -d '{"username":"admin","password":"…"}' http://127.0.0.1:8642/api/v1/auth/login
curl -s -b cookies.txt -H 'content-type: application/json' \
  -d '{"name":"mcp","scopes":["read:events"]}' http://127.0.0.1:8642/api/v1/auth/tokens
# then, for example in Claude Code:
claude mcp add --transport http vidette http://127.0.0.1:8642/api/v1/mcp \
  --header "Authorization: Bearer vd_…"
```

## Budgets

| | |
|---|---|
| Compute | none here; the model's server does the work: 1 to 4 calls a question |
| Storage | one row per answer (`telegram_messages`, kind `answer`) |
| Network | to the model's server (local by default); to Telegram, the answer and any video |
| Latency | quick answers: seconds; others: the model's queue, about 1 to 3 minutes on a shared server |
