# Spec Delta

## MODIFIED Requirements

### Requirement: Configured MCP servers
alfred SHALL connect to each MCP server in the configuration over Streamable HTTP, sending a bearer token chosen for the turn's user: that user's `mcp_tokens` entry for the server when set, otherwise the server's own `token_env`. Servers not in the configuration SHALL NOT be contacted.

#### Scenario: Two servers configured
- **WHEN** mcparr and cookbook are configured with URLs and token variables
- **THEN** the model is offered tools from both, and each request to a server carries that server's token

#### Scenario: Per-user token
- **WHEN** `ana` has `mcp_tokens = { mcparr = "ALFRED_MCP_TOKEN_MCPARR_ANA" }`
- **THEN** listing and calls to mcparr on `ana`'s turns carry that variable's value, and on other users' turns carry `ALFRED_MCP_TOKEN_MCPARR`'s

## ADDED Requirements

### Requirement: Servers outside a user's allow-list are not contacted
On a user's turn, a server for which no allow-list entry of that user can match (no `*` entry and no entry starting with `<server>_`) SHALL NOT be contacted, SHALL contribute no instructions, and SHALL NOT be reported as unavailable.

#### Scenario: No cookbook access
- **WHEN** `ana`'s allow-list has no `cookbook_` entry and no `*`
- **THEN** cookbook is not contacted on her turns and her system prompt has no cookbook instructions or unavailable note

### Requirement: Instructions only with offered tools
A server's instructions SHALL be given to the model only when at least one of its tools is offered on the turn after the allow-list is applied.

#### Scenario: Allowed tool missing
- **WHEN** `ana`'s allow-list has only `mcparr_add_medai` (a typo) for mcparr
- **THEN** none of mcparr's tools are offered and mcparr's instructions are not sent
