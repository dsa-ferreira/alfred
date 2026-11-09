# Tasks

## 1. Dependencies and configuration

- [x] 1.1 Add `aiobotocore`, `pypdfium2` and `Pillow` to dependencies and `moto[s3,server]` to dev dependencies; verify `make install` succeeds and `make image` builds with no new system packages
- [x] 1.2 Add `[ai] vision` and the `str | BrainEntry` form of `[ai.models]` (`extra="forbid"`, duplicate check on resolved tags) per design D9; verify "Vision declared", "Startup model without an alias declares vision", "Same tag twice, one as a table" and "Unknown key in a brain table" in `tests/config/test_config.py`
- [x] 1.3 Add the optional `[attachments]` section (`endpoint_url`, `bucket`, `region="garage"`, `max_bytes=20000000` capped at 20 MB, `inline_turns=1`, `first_page_chars=4000`, `read_chars=8000`, `image_max_px=1536`, `album_wait_s=2`) and require `ALFRED_S3_ACCESS_KEY_ID` / `ALFRED_S3_SECRET_ACCESS_KEY` only when it is set; verify "Attachments off by default", "Attachment defaults", "Size limit above Telegram's" and "Object store key missing" in `tests/config/test_config.py`, and that the secret key never appears in errors in `tests/config/test_no_secret_leaks.py`
- [x] 1.4 Document `[attachments]`, `[ai] vision` and the table form of `[ai.models]` (gemma and bonsai with `vision = true`) in `alfred.toml.example`; verify `tests/config/test_example.py` passes

## 2. Brains

- [x] 2.1 Make `Brain` `(name, tag, vision)` and build it from the resolved config in `agent/brains.py` (design D9); verify vision per brain, including the unaliased startup brain, in `tests/agent/test_brains.py`

## 3. Storage and data model

- [x] 3.1 Add the `Attachment` and `AttachmentPage` models and migration `0004` (design D1); verify upgrade and downgrade in `tests/db/`
- [x] 3.2 Add `alfred/attachments/store.py`: an `ObjectStore` protocol (put, get, list with age, delete) and the aiobotocore implementation with path-style addressing, `when_required` checksums and fixed timeouts (design D10); verify put/get/list/delete against `moto`'s threaded server in `tests/attachments/test_store.py`, and that a failed request's log line has no secret
- [x] 3.3 Add an in-memory `ObjectStore` fake in `tests/helpers.py` for the other tests, with a switch that makes it fail

## 4. Reading files

- [x] 4.1 Add `attachments/images.py` (design D6): validate the accepted formats with an explicit pixel limit, read the size, and normalise for the model (`exif_transpose`, scale to `image_max_px`, JPEG or PNG with transparency) in a thread; verify "Large photo" (4000x3000 → 1536x1152, stored bytes unchanged), a rotated EXIF photo, a transparent PNG staying PNG, and a truncated file refused, in `tests/attachments/test_images.py`
- [x] 4.2 Add `attachments/pdf.py` (design D7): open under one lock in a thread, refuse encrypted, damaged or zero-page files, extract every page's text with the 16-character "has text" rule, render a page scaled to `image_max_px`; verify a text PDF, a scanned (image-only) PDF, a password-protected one, and rendering of page 2's size, with small PDFs generated in the test, in `tests/attachments/test_pdf.py`

## 5. Receiving in Telegram

- [x] 5.1 Add `notices.unsupported`, `too_large`, `unreadable` and `not_kept` (design D12); verify each with the default and a custom address in `tests/test_notices.py`
- [x] 5.2 Add the intake (`attachments/intake.py`): check the size before downloading, download, validate or extract, PUT, then one transaction with the queue item, attachments and pages (design D2); `Queue.enqueue_owner` takes attachments. Verify "Photo with caption", "PDF without caption", "Image sent as a file", "Too large" (no download attempted), "Password-protected PDF" and "Bucket unreachable" (nothing queued) in `tests/attachments/test_intake.py`
- [x] 5.3 In `telegram/bot.py`, route photos, `image/*` documents and `application/pdf` documents to the intake when attachments are configured, use `unsupported` for other types and `text_only` when attachments are not configured, and collect albums by `media_group_id` with the `album_wait_s` timer (design D3); verify "Supported message types" scenarios and "Three photos in one album" (one queue item, parts in order, the caption from whichever part has it, a refused part notified and the rest queued) in `tests/telegram/`
- [x] 5.4 Update the README's description of what the owner can send

## 6. History, transcripts and turns

- [x] 6.1 Have `History.append` return the entry id, and set `attachments.entry_id` when the worker records the owner entry, idempotent on retry (design D2); verify in `tests/test_queue.py` that a retried turn links the attachments once
- [x] 6.2 Load attachments with `History.recent()` and append handles in `to_messages()` (design D4); verify "PDF handle" and an owner entry with two images in `tests/test_history.py`
- [x] 6.3 Render handles in `memory/transcript.py` (design D11); verify "Recall shows handles" and that a summary transcript has no PDF text, in `tests/memory/`
- [x] 6.4 Add `ModelImage` and `ToolOutcome.images`, have the turn send list `content` for tool results with images, and store `[N image(s)]` in the excerpt instead of base64 (design D5); verify with a fake toolset in `tests/agent/test_turn.py`
- [x] 6.5 In the agent, upgrade the inline window's owner messages to content parts, with a per-turn byte cache, the vision check and the `(not available right now)` fallback (design D4); verify "Photo question", "Text PDF", "Scanned PDF", "Follow-up question", "Photo on a text-only brain", "PDF on a text-only brain" and a store failure in `tests/agent/test_turn.py`
- [x] 6.6 Count image parts as `N images` in `agent/context.py` (design D11); verify in `tests/agent/test_context.py` that base64 is not counted as characters

## 7. Attachment tools

- [x] 7.1 Add `AttachmentTools` (owner turns; the view tool only on a vision brain; prompt notes) with the access query (design D8); verify "Look again", "PDF page", "Page out of range", "From a recalled conversation", "From an unrelated conversation", "Store down on a follow-up" and "Fallback mid-turn" in `tests/attachments/test_tools.py`
- [x] 7.2 Implement `alfred_read_document` page ranges, `--- page N ---` blocks, the `read_chars` cut with `(continue from page M)`, and the no-text marker; verify "Read pages", "Long range", "Not a PDF" and "Larger than the global limit" in `tests/attachments/test_tools.py`
- [x] 7.3 Add the toolset to `build_toolsets` after `MemoryTools`, only when attachments are configured; verify the order, and a whole owner turn that views an attachment, with `tests/agent/turn_helpers.py`
- [x] 7.4 Verify "Owner turn on a vision brain" (handles and both tools explained) and a text-only brain (no view tool mentioned) in `tests/agent/test_prompts.py` or the toolset tests

## 8. Orphan sweep and wiring

- [x] 8.1 Add `attachments/sweeper.py`: at startup and then every 24 h, delete objects older than 24 h under `attachments/` with no row, and log errors without stopping (design D10); verify "Crash between upload and record", a fresh orphan kept, a referenced object kept and a store error survived, in `tests/attachments/test_sweeper.py`
- [x] 8.2 In `app.serve()`, build the store, intake and toolset when `[attachments]` is set, start the `attachments` task, close the S3 client on shutdown, and name attachments in the startup log line; verify with and without `[attachments]` in `tests/test_serve.py`
- [x] 8.3 Run `make lint` and `make test` and verify both pass

## 9. End to end

- [x] 9.1 Against `ai.lan` and the real Garage, from a Telegram chat with a local alfred on `gemma`: send a receipt photo ("what's the total?"), then "and the date?" (expect `alfred_view_attachment`); send a multi-page text PDF and ask about page 3 (expect `alfred_read_document`); send a scanned single-page PDF; send an album of two photos; switch to `bonsai` and ask about the receipt again. Verify the answers, the tool calls in `turn_tool_calls`, the image counts in the context log, and that the objects exist in the bucket.
- [x] 9.2 End the conversation, start a new one, recall it, and ask about the PDF again; verify the tool reaches it. Then ask about an attachment number from a conversation that was not recalled, and verify the refusal.

## Workflow follow-up

- Bump the version and write the handover update with the `homelab-handover` skill: migration `0004`; the bucket and a key limited to it; `ALFRED_S3_ACCESS_KEY_ID` / `ALFRED_S3_SECRET_ACCESS_KEY`; `[attachments]` with the Garage endpoint and bucket; `vision = true` on `gemma` and `bonsai`; that Postgres backups no longer cover attachment files.
- Archive the change after review.
