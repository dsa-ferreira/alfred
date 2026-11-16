# Tasks

Every group ends with `make lint` and `make test` green and its own
commit(s). No task changes what a test asserts.

## 1. Layout rule and reorder

- [x] 1.1 Add the layout rule (design D1) to `AGENTS.md` under a "Code layout" section, and verify it states the module order, the class order, what "public" means and the definition-order exception
- [x] 1.2 Write `scripts/check_layout.py` with checks 1-4 of design D2 (check 5 comes in group 3) and run it on a few small sample files in a temporary directory; verify it flags each kind of violation with `path:line` and accepts a compliant file
- [x] 1.3 Rename top-level names to match usage (internal gets `_`, imported elsewhere loses it) across `src/alfred/` and `migrations/`, updating test imports; verify check rules 1-2 pass and `make test` is green
- [x] 1.4 Rename methods by the same rule where something outside the class calls them, or nothing does (review by hand, design D1); verify `make test` is green
- [x] 1.5 Reorder every module and class (design D1), only moving code in this commit; verify check rules 3-4 pass and `git diff --color-moved` shows only moves
- [x] 1.6 Add `scripts/check_layout.py` to `make lint`; verify `make lint` passes and fails after a deliberate violation, which is then removed
- [x] 1.7 Add `.git-blame-ignore-revs` with the hashes of the rename and reorder commits and document `git config blame.ignoreRevsFile .git-blame-ignore-revs` in `AGENTS.md`; verify `git blame` on a reordered file shows the original commits

## 2. Comment cleanup

- [x] 2.1 Remove the `(change X, design DY)`, `(design DY)` and `(DY)` references from `src/alfred/`, keeping each comment's reason (design D4); verify `grep -rEn "change [a-z]+-[a-z-]+|design D[0-9]+|\(D[0-9]+\)" src/alfred` finds nothing
- [x] 2.2 Rewrite module docstrings that narrate history (`agent/turn.py`, `app.py`, `queue.py` and others found by 2.1) to describe the module as it is; verify by reading each changed docstring against its code
- [x] 2.3 Add the commits of 2.1-2.2 to `.git-blame-ignore-revs`; verify `make lint` and `make test` pass

## 3. History split and import cycles

- [x] 3.1 Move `to_messages`, `user_text`, `stamped`, `strip_stamp`, `format_stamp` and `Inline` from `history.py` to `agent/messages.py`, along with their tests; verify `history.py` no longer imports `memory` or `reminders`
- [x] 3.2 Move `InlineAttachments` and `ImageCache` from `attachments/inline.py` into `agent/`, keeping `load_image` in `attachments`; verify no module in `attachments/` imports `history`
- [x] 3.3 Move `agent/brains.py` to `alfred/brains.py`; verify `attachments` no longer imports `agent`
- [x] 3.4 Give `attachments.intake` an `Enqueue` protocol in place of `Queue`; verify `attachments` no longer imports `queue`
- [x] 3.5 Split `queue.py` into `queue.py` (`Queue`, `QueueActivity`) and `worker.py` (`Worker` and its protocols), moving tests to match; verify `api` and `telegram` don't import `worker` or `agent`
- [x] 3.6 Add check 5 of design D2 (no two packages import each other) to `scripts/check_layout.py`; verify it passes now and fails on a deliberate back-import, which is then removed
- [x] 3.7 Update `AGENTS.md` for the moved names (the "Adding a tool group" section mentions `turn.py`); verify every module path it names exists

## 4. Required dependencies

- [x] 4.1 Add the fakes the worker and agent tests need to `tests/fakes.py` (input requests, question outbox, reminder lookup, typing, facts, inline attachments); verify existing tests still pass unchanged
- [x] 4.2 Make `Worker`'s collaborators required (design D6) and delete the branches for missing ones (like "not wired (only in tests)"); verify `make test` is green and `grep -n "is None\|is not None" src/alfred/worker.py` shows no collaborator checks
- [x] 4.3 Make `Agent`'s collaborators required (`brains`, `attachments`, `facts`) the same way; verify `make test` is green
- [x] 4.4 Look for other `| None = None` collaborators that exist only for tests (`grep -rn "in tests" src/alfred`) and make them required; verify the grep finds no such comments

## 5. Parked turns

- [x] 5.1 Add a typed `Checkpoint` with `to_json`/`from_json` (design D7), with tests that round-trip it and read stored 0.16.0 and 0.16.1 JSON; verify `to_json` writes the same keys as before
- [x] 5.2 Use `Checkpoint` in `Waiting`, `Agent` and the resume path in place of `resume.turn["..."]`; verify `grep -rn 'resume.turn\[' src/alfred` finds nothing and the input request tests pass
- [x] 5.3 Replace `History.recent()`'s optional filters with two methods (current window, a checkpoint's window); verify the history and turn tests pass with only call sites changed
- [x] 5.4 Move `_resumed_before`, taking the answered request, and `_park` from `Worker` into one class in `alfred/inputs/`, with their tests; verify `Worker` has no parking logic left and the input request tests pass

## 6. The turn attempt

- [x] 6.1 Split `Agent._attempt` into `_build_context()` and `_loop()` (design D8); verify neither is over about 50 lines and `make test` is green

## 7. Turn kinds

- [ ] 7.1 Replace `TurnInput` in `tools.base` with `ChatTurn`, `AskTurn` and `ReminderTurn`, and make `TurnInput` their union (design D9); move the input message and system prompt flags onto them; verify `tools.base` still imports nothing else from alfred at runtime
- [ ] 7.2 Change the toolsets' kind checks (`chat_only` and others) to `isinstance`; verify `grep -rn 'kind == "' src/alfred` finds only the mapping in `queue.py`
- [ ] 7.3 Add per-kind handlers next to the worker (delivery and recording, failure notice, typing indicator) and make `Worker._complete` and `_turn_failed` dispatch to them; verify the worker tests pass with only construction changed
- [ ] 7.4 Map the stored queue kind `"user"` to chat in one place in `queue.py`, and use "chat" everywhere else in the code; verify no migration is added and `make test` is green
- [ ] 7.5 Update `AGENTS.md` where it names `TurnInput` or `turn.kind`; verify it matches the code

## 8. Startup

- [ ] 8.1 Split `app.serve()` into `build_services`, `run_tasks` and the startup log line, closing resources through an `AsyncExitStack` (design D10); verify the app tests pass, and that `make run` starts and stops cleanly on Ctrl-C

## 9. Final checks

- [ ] 9.1 Re-run the package import graph and the layout check over the whole tree; verify no package pair imports each other and `make lint` and `make test` pass
- [ ] 9.2 Build the image (`make image`, after bumping the version as usual) and run it against a parked turn created by the previous image; verify the turn resumes

## Workflow follow-up

- Archive the change once every group is merged.
