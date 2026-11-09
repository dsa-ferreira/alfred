# Design

## Context

See proposal.md for motivation and `specs/attachments` for the behavior.

- The bot (`telegram/bot.py`) handles every owner message in one handler.
  It queues `message.text` and answers anything else with
  `notices.text_only`. aiogram runs the handlers of concurrent updates as
  tasks.
- A queue item (`queue_items`) carries only text. The worker records the
  owner entry at turn start (`history.append("owner", …, queue_item_id)`)
  and builds `TurnInput` from the item.
- `History.recent()` returns `HistoryEntry` rows, and `to_messages()` turns
  them into chat-completions messages whose `content` is always a string.
  `memory/transcript.py` renders entries again for summaries and recalls.
- `ModelClient.complete()` posts `messages` as given. Only the reply's
  `content` must be a string, so request messages can already use content
  parts.
- Toolsets (`tools/base.py`) return `ToolOutcome(ok, text)`, and the turn
  wraps it as `{"role": "tool", "content": text}`. Each toolset cuts its own
  results to `[ai] tool_result_max_chars`. Web fetch opts out and sizes its
  own chunks.
- `Brain` is `(name, tag)`. `[ai.models]` is `dict[alias, tag]`.
- Spike, run against `ai.lan` before this change, for `gemma4:26b-a4b-it-qat`
  and `bonsai-2:27b` on `/v1/chat/completions`:
  - An image in a `user` message as an `image_url` part with a base64
    `data:` URL: read correctly by both brains.
  - An image in a `tool` message with list `content` (text part plus an
    `image_url` part): read correctly by both brains.
  - With only `[attachment #7: photo]` in history and a view tool offered,
    both brains called the tool on all 3 of 3 runs.
  - A 240x160 PNG cost about 85 prompt tokens on gemma.

## Goals / Non-Goals

**Goals:**
- The cost per turn stays flat however many attachments the conversation
  holds: only the arriving turn carries image data.
- A file is never lost once the owner has had no refusal notice, and a
  database row never points at a missing object.
- Text-only deployments (no `[attachments]`) behave exactly as before and
  need no object store.

**Non-Goals:**
- Searching PDF text in conversation memory. Keyword and vector search
  stay over entries and summaries.
- Images in MCP tool results (`ImageContent`), voice notes, office
  documents.
- Deleting attachments. Nothing deletes a conversation, so nothing deletes
  its files, apart from orphan objects.
- OCR. A scanned page is read by the vision model from its image.

## Decisions

### D1. Bytes in the object store, everything else in Postgres

The original file goes to the bucket under `attachments/<uuid4>`. The key
never contains the file name. Postgres holds the metadata and each PDF
page's text, so handles, the access check and `alfred_read_document` never
touch the store.

```
attachments
  id              bigint pk          -- the number in "#7"
  queue_item_id   fk queue_items     -- set at enqueue
  entry_id        fk entries, null   -- set when the owner entry is recorded
  position        int                -- order within the message (albums)
  kind            'image' | 'pdf'
  mime, file_name (null), size_bytes, object_key unique
  width, height   (images)
  page_count      (pdfs)
  created_at

attachment_pages
  attachment_id fk, page int, text text, primary key (attachment_id, page)
```

A page "has text" when its stripped text is at least 16 characters, which
keeps stray page numbers from counting as a text layer. A PDF's handle says
`text` when page 1 has text, else `scanned`.

*Alternatives:* `bytea` in Postgres, which is simpler and backed up with
everything else, but the owner chose Garage. A filesystem volume would tie
alfred to one host.

### D2. Upload first, then one transaction

```
bot handler (per message, or per album after D3's wait)
  check file_size <= max_bytes          (before downloading)
  download to memory                    (<= 20 MB)
  validate + extract, in a thread       (D6, D7)
  PUT each object                       -> failure: storage notice, stop
  one transaction: queue_items row + attachments rows (+ attachment_pages)
  queue.signal.set()
```

A crash after a PUT and before the commit leaves an orphan object, which
D10 removes. The commit is what makes the message exist, so the database
never refers to a missing object. When the worker records the owner entry,
it sets `attachments.entry_id` in the same transaction (an `UPDATE …
WHERE queue_item_id = :id`). A retried turn finds it already set.
`History.append` returns the entry id for this.

### D3. Albums: collect by `media_group_id`

Telegram delivers an album as separate messages that share a
`media_group_id`. The bot keeps an in-memory buffer per group. Each part
restarts a timer of `album_wait_s` (default 2 s), and when the timer fires
the group is processed as one message: the parts in `message_id` order,
and the caption from whichever part has one. A refused part gets its own
refusal notice and the rest are still queued. A restart during the wait
loses the buffered album. This is rare, and Telegram won't redeliver it,
so the owner simply sends it again.

*Alternative:* one turn per album part. Rejected because the owner would
get several replies to one question.

### D4. History carries handles; the turn adds images

`History.recent()` also loads each owner entry's attachments (one query by
`entry_id`), as `HistoryEntry.attachments`. `to_messages()` stays text
only. It appends the handles to the owner content:
`[Mon 9 Nov 14:05] what's the total? [attachment #7: photo "receipt.jpg"]`.

The agent then upgrades the owner messages of the newest `inline_turns`
owner entries that have attachments. Their `content` becomes a list: the
text part first, then one `image_url` part per image, or per PDF page 1
without text, as `data:<mime>;base64,…`. A PDF's page 1 text goes into the
text part, after its handle, cut to `first_page_chars`. On a brain without
vision there are no image parts, and the handle gets
`(this brain can't see images)`.

Bytes are fetched once per turn and cached for the turn, so a fallback
attempt doesn't fetch them again. A fetch failure gives
`(not available right now)` on the handle and a warning in the log.

*Alternative:* store image parts in history. Rejected: entries stay text,
so summaries, search and the tsvector index are unaffected.

### D5. Images in tool results

`ToolOutcome` gains `images: tuple[ModelImage, ...] = ()`. A `ModelImage`
holds the bytes and mime type, already scaled down per D6. When a result
has images, the turn sends
`{"role": "tool", "tool_call_id": …, "content": [{"type": "text", …}, {"type": "image_url", …}]}`,
which is spike case C. `turn_tool_calls.result_excerpt` stores the text
plus `[1 image]`, never base64. `alfred_read_document` is not cut to
`tool_result_max_chars` and sizes its own output with `read_chars`, as web
fetch does.

*Alternative:* return text from the tool and add a synthetic `user`
message with the image (spike case D). It worked too, but it breaks the
assistant → tool pairing and adds a fake owner turn. It stays a fallback
if another server rejects images in tool messages.

### D6. Images: Pillow, normalised on the way to the model

On receipt, Pillow opens the file to check it is an accepted format (JPEG,
PNG, WebP, GIF first frame) and reads its size. `MAX_IMAGE_PIXELS` is set
explicitly so decompression bombs are refused. The stored file is the
original. On the way to the model, the image is turned upright with
`exif_transpose`, scaled down so its longer side is at most `image_max_px`,
and encoded as JPEG (quality 85), or PNG when it has transparency. This
runs in a thread.

Telegram "photos" arrive already compressed. The largest size is taken
(`message.photo[-1]`).

### D7. PDFs: pypdfium2

On receipt, in a thread: open the PDF (refuse it when it is encrypted or
can't be parsed), count its pages (refuse 0), and extract every page's
text into `attachment_pages`. Pages are rendered on demand for
`alfred_view_attachment` and page 1 inline: the scale is chosen so the
longer side is `image_max_px`, and the image is encoded per D6. pdfium is
not thread-safe, so every pypdfium2 call goes through one lock (one worker
thread). pypdfium2 ships manylinux wheels, so the Docker image needs no
system packages.

*Alternatives:* PyMuPDF (AGPL); pypdf plus poppler's `pdftoppm` (two
pieces, and system packages in the image).

### D8. Tools and the access rule

A new `AttachmentTools(LocalToolset)` in `alfred/attachments/tools.py`
holds the store, `Brains` and the config. It is offered on owner turns and
added to `build_toolsets` after `MemoryTools`. On `open`, its
definitions are `alfred_read_document`, plus `alfred_view_attachment` when
the current brain has vision. Its `prompt_notes` explain handles and the
offered tools (spec "Attachment tools explained"). `call` checks
`brains.current.vision` again for a view, which covers a fallback inside
the turn.

Access check, one query:

```
attachment.entry.conversation_id = current
  OR attachment.entry.conversation_id IN (
       SELECT recalled_conversation_id FROM entries
       WHERE conversation_id = current AND kind = 'recall')
```

A recall only brings back the handles that appear in its transcript, so a
conversation recalled inside another conversation is reachable only when
it is itself recalled.

`alfred_read_document(id, pages)` takes `"4"` or `"3-5"`, or no range for
"from page 1". It returns `--- page N ---` blocks up to `read_chars`, then
`(continue from page M)` when it is cut. A page without text shows
`(no text on this page; use alfred_view_attachment)`, or on a brain without
vision `(no text on this page)`.

### D9. Vision flag in config

`[ai.models]` values become `str | BrainEntry`, where `BrainEntry` is
`{ model: str, vision: bool = false }` with `extra="forbid"`.
`[ai] vision: bool = false` applies to the `[ai] model` brain when no alias
names it. `Brain` becomes `(name, tag, vision)`. The duplicate-tag check
compares the resolved tags. The system prompt's brain line is unchanged.
Vision only decides what is sent and offered.

### D10. Object store client and orphan sweep

`aiobotocore` with one client for alfred's lifetime: `endpoint_url`,
`region_name=region`, `addressing_style="path"`,
`request_checksum_calculation="when_required"` and
`response_checksum_validation="when_required"`. The last two avoid the
newer default checksum headers, which some S3-compatible servers reject.
Connect and read timeouts are fixed at 10 s and 60 s. Credentials come from
`ALFRED_S3_ACCESS_KEY_ID` / `ALFRED_S3_SECRET_ACCESS_KEY` as `SecretStr`.
Errors are logged with the error code and the key, never the credentials.

A new `attachments` background task (like `memory` and `reminders`) runs
once at startup and then every 24 h. It lists `attachments/` (paginated),
and deletes objects older than 24 h whose key has no row. Errors are
logged, and the task waits for the next round. It never stops alfred.

*Alternatives:* `aioboto3` (a wrapper over the same library), or
hand-signed SigV4 over httpx (less code to depend on, more code to own).

### D11. Context-size log and transcripts

`context_sizes` counts `image_url` parts as `N images` and leaves their
base64 out of the character counts, so the log stays meaningful.
`memory/transcript.py` renders an owner entry as
`Owner: <text> [attachment #7: …]`. `TranscriptEntry` gets the handles from
the same attachment query.

### D12. Notices

- `text_only`: unchanged; used when `[attachments]` is not set.
- `unsupported`: "I can read text, images and PDFs, {address}."
- `too_large(limit)`
- `unreadable`: covers an image that can't be decoded, and a PDF that is
  damaged, password-protected or has no pages.
- `not_kept`: storage or download failure, asking for the file again.

All are in the persona's fixed-notice voice.

## Risks / Trade-offs

- [Real photos and dense pages cost far more tokens than the spike image.]
  → `image_max_px` is tunable, the context log counts images, and only the
  arriving turn carries images by default.
- [The Q2 `bonsai-2` misreads detail in real documents.] → Try it before
  relying on it. Its vision flag can be turned off without a code change,
  and it then reads PDF text only.
- [The model picks the wrong attachment when there are several.] → Handles
  carry the file name and kind. The tool error for a wrong number lists
  none of the other ids, to keep it short. Check this in the end-to-end
  task.
- [Album buffer lost on restart, or a text sent during the 2 s wait is
  queued before the album.] → Accepted. Both are rare, and both are visible
  to the owner.
- [Postgres backups no longer cover the whole conversation.] → The handover
  says so. Backing up the bucket is on the homelab side.
- [Garage rejects a request shape (checksums, addressing).] → D10's client
  settings. The end-to-end task uploads and reads back against the real
  Garage.

## Migration Plan

1. Migration `0004` creates `attachments` and `attachment_pages`. It only
   adds tables, so rolling back means running the previous image (the
   tables are ignored) or downgrading `0004`.
2. Homelab: create the bucket and a key limited to it, set the two
   `ALFRED_S3_*` secrets, add `[attachments]` with `endpoint_url` and
   `bucket`, and set `vision = true` on `gemma` and `bonsai` in the real
   `alfred.toml`. Without `[attachments]`, the release behaves as 0.9.0.
3. Version bump and handover update through the `homelab-handover` skill.

## Open Questions

- The Garage endpoint hostname and port, and whether Garage is backed up.
  These are homelab values for the handover. They don't change the design.
