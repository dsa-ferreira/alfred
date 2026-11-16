# Spec Delta

## ADDED Requirements

### Requirement: Servers may ask the user
alfred SHALL tell every MCP server it connects to that it can show form questions to the user (form elicitation), and SHALL NOT claim URL mode, sampling or roots. A tool call the server answers with a request for input SHALL be handled as `user-input-requests` says, never by answering the server without the user.

#### Scenario: Server needs a confirmation
- **WHEN** a server's tool asks the user to confirm before it acts
- **THEN** the call doesn't fail as unsupported; the turn waits for the user's answer

#### Scenario: Server asks for sampling
- **WHEN** a server answers a call by asking alfred to run a model request for it
- **THEN** the call ends with an error result for the model, and nothing is sent back to the server
