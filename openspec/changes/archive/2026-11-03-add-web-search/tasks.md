# Tasks

## 1. Configuration and dependency

- [x] 1.1 Add `WebConfig` (`[web]`: `searxng_url`, `search_results`, `fetch_page_chars`, `fetch_timeout_s`, `fetch_max_bytes`) to `config.py` per design D9, and change the `[ai] max_requests_per_turn` default to 12; verify "Web defaults", "Change the fetch chunk size" and "Invalid SearXNG URL" in `tests/config/test_config.py`
- [x] 1.2 Document `[web]` and the new request limit default in `alfred.toml.example` and the README config table; verify `tests/config/test_example.py` passes
- [x] 1.3 Add `trafilatura` to `pyproject.toml` with `uv add`, check its `extract` API (Markdown output, links, tables, metadata title) against its documentation, and verify `make install` succeeds

## 2. URL allowlist

- [x] 2.1 Add `web/allowlist.py`: URL normalisation (http/https only, lower-case host, default port and fragment removed), URL extraction from text with trailing punctuation stripped, and a per-turn allowed set (design D3); unit tests in `tests/web/test_allowlist.py` for "Fragment ignored", "Other scheme" and extraction from pasted text with punctuation

## 3. Search

- [x] 3.1 Add `web/search.py`: SearXNG JSON query with optional `time_range` and `categories`, result shaping (top N, deduplicated, title/URL/date/snippet, answers and infoboxes first, failed-engines line, snippets shortened to fit `tool_result_max_chars`) per design D4, returning the text and the listed URLs; verify every "Web search through SearXNG", "Search extras" and "Search failures go to the model" scenario in `tests/web/test_search.py` with `respx`, using a fixture modelled on a real `oracle.lan` response

## 4. Fetch and extraction

- [x] 4.1 Add `web/fetch.py`: guarded GET with manual redirects (max 5), a non-global address check on every hop (IPv4-mapped IPv6 unwrapped), total timeout, streamed size cap and no cookies (design D5); verify "LAN host in a search result", "Redirect to a private address", "Ordinary redirect", "Huge download", "Slow host" and "Not found" in `tests/web/test_fetch.py` with `respx` and a stubbed resolver
- [x] 4.2 Add `web/extract.py`: HTML through trafilatura in a thread (Markdown, links, tables, title), plain text as is, other types refused, empty result as "no readable content" (design D6); verify "Article page", "Image" and "Page built by JavaScript" in `tests/web/test_extract.py` with small HTML fixtures

## 5. Web tools

- [x] 5.1 Add `web/tools.py` with `WebTools`: both tool definitions (category and time range as enums, `start` as integer), argument parsing, search adding listed URLs to the turn's allowlist, fetch checking the allowlist before any request, per-turn page cache, paging with the header first and paragraph-aware chunk ends, untrusted-content markers, and error results for every failure (design D2, D3, D7); verify every "Fetch only allowed URLs", "Paged page text", "Fetch result size independent of the global limit", "Fetched text is marked untrusted" and "Fetch failures go to the model" scenario in `tests/web/test_tools.py`
- [x] 5.2 Add the web operating rules to `OPERATING_RULES` in `prompts.py` (design D8); verify the "Web rules" scenario for owner and `/ask` turns and with a custom persona in `tests/agent/test_prompts.py`

## 6. Agent loop

- [x] 6.1 In `agent/turn.py`, take an optional `WebTools` in `Agent`, offer both tools in owner and `/ask` turns, create the per-turn web state in each attempt seeded from `owner` history entries in the window and the `/ask` prompt, and route the two tools to it without the global result cut; verify "Owner turn", "Ask turn", "URL the owner pasted", "URL the owner pasted in an earlier message", "Search result from an earlier turn", "URL from alfred's own reply" and "Notice or recall" in `tests/agent/test_turn.py`
- [x] 6.2 Verify "Room for web research" in `tests/agent/test_turn.py`: with default config, a scripted model doing search, two fetches, a read-on, a second search, a fetch and an answer completes within 12 requests

## 7. Wiring and docs

- [x] 7.1 Build `WebTools` from `config.web` in `app.serve`, share one `httpx.AsyncClient` for search and fetch, close it on shutdown, and log the SearXNG URL at startup; verify in `tests/test_serve.py`
- [x] 7.2 Describe web search and fetch, the URL allowlist and the private-address block in the README; verify the README's config table matches `alfred.toml.example`
- [x] 7.3 Run `make lint` and `make test` and verify both pass
- [x] 7.4 Against the real `oracle.lan`, run a search and a fetch of one result through `WebTools` from a Python shell and verify the shaped output and the paging header look right

## Workflow follow-up

- Bump the version and write a handover update (outbound internet access, `oracle.lan`, `[web]` keys, explicit `max_requests_per_turn = 8` to remove or raise) with the `homelab-handover` skill.
- Archive the change after review.
