# Proposal

## Why

alfred can only answer from what the model already knows and from the
homelab's own services. Questions about current events, today's weather, a
new release or the content of a link the owner pastes get a guess or a
refusal. The owner already runs a SearXNG instance on `oracle.lan`, so alfred
can search the web and read pages without any outside API or key.

## What Changes

- Two new local tools, offered in owner turns and in `/ask` turns:
  - `alfred_web_search`: queries SearXNG's JSON API and returns a short,
    shaped list (title, URL, snippet, date when known), plus SearXNG's direct
    answers and infoboxes when present, and a note when engines failed.
    Optional time range and category arguments ("news from this week").
  - `alfred_web_fetch`: downloads one page, extracts its main content as
    light Markdown, and returns it one chunk at a time. The model reads
    further with a start offset; the position line comes first in the
    result.
- **Strict URL allowlist:** fetch only opens a URL that appeared in a search
  result earlier in the same turn, or in one of the owner's own messages in
  the turn's context (in `/ask` turns, in the service's prompt). URLs in
  alfred's earlier replies and links inside fetched pages are not allowed;
  the model has to search for them.
- Every request fetch makes, including each redirect, is refused if the host
  resolves to a private, loopback, link-local or otherwise non-public
  address.
- Fetched text is framed as untrusted page content, and the operating rules
  tell the model never to follow instructions found in it.
- Fetch has its own chunk size, separate from `[ai] tool_result_max_chars`,
  sized to stay safe for the smallest brain's context.
- No JavaScript rendering: pages that need a browser come back as "no
  readable content".
- New `[web]` config section: SearXNG URL, number of search results, fetch
  chunk size, fetch timeout and download size cap. The defaults point at
  `http://oracle.lan`.
- `[ai] max_requests_per_turn` default goes from 8 to 12, so a turn has
  room for search, a few fetches and the answer.

## Capabilities

### New Capabilities

- `web-tools`: searching the web through SearXNG and reading pages, with the
  URL allowlist, network restrictions and size control that keep it safe for
  a small local model.

### Modified Capabilities

- `agent-loop`: the operating rules explain the web tools and that fetched
  page text is untrusted; the default request limit per turn rises to 12.
- `service-configuration`: the list of tunables gains the `[web]` settings.

## Impact

- Code: new `src/alfred/web/` package (SearXNG client, page fetcher and
  extractor, allowlist), `src/alfred/agent/turn.py` (offer and route the two
  tools in both turn kinds, track allowed URLs per turn),
  `src/alfred/agent/prompts.py` (operating rules), `src/alfred/config.py`
  (`[web]`, new request limit default), `src/alfred/app.py` (wiring),
  `alfred.toml.example`, README.
- Dependencies: `trafilatura` for main-content extraction.
- External services: `http://oracle.lan` (SearXNG, JSON format already
  enabled, no auth). Outbound HTTP(S) from alfred's container to the public
  internet, for fetch.
- Model load: web turns take more model requests (typically 2 to 5) and
  carry more context; each fetch chunk is about 2.5k tokens.
- Deployment: a release and a handover update (outbound internet access,
  reachability of `oracle.lan`, the `[web]` keys, the new request limit).
