# Spec Delta

## MODIFIED Requirements

### Requirement: OpenAI-compatible model endpoint
alfred SHALL call the model through an OpenAI-compatible chat completions API, using the base URL from configuration and the current brain's model (see `model-selection`), which starts as the configured model. Changing model servers SHALL need only a configuration change.

#### Scenario: Default Ollama endpoint
- **WHEN** the configuration sets base URL `http://ai.lan` and model `gemma4:12b-it-qat`
- **THEN** turns are sent to that endpoint's chat completions API with that model

#### Scenario: Switch servers
- **WHEN** the base URL and model are changed to another OpenAI-compatible server and alfred is restarted
- **THEN** turns go to the new server with no code change

#### Scenario: Another brain chosen
- **WHEN** the owner has switched to the brain `bonsai` (`bonsai:8b`)
- **THEN** turns are sent to the same base URL with model `bonsai:8b`
