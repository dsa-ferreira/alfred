# Spec Delta

## MODIFIED Requirements

### Requirement: Fixed notices in the persona's voice
The notices alfred sends to the owner without the model (only text is supported, the request failed, the AI is unreachable, the AI became unreachable mid-request, the request was stopped after too many steps, some actions may have partly completed, and the chosen brain failed so alfred went back to the startup brain) SHALL use a formal butler register and end with the configured form of address, used exactly as configured. They SHALL NOT be configurable.

#### Scenario: Failure notice with the default address
- **WHEN** an owner turn fails with no tool having run and no form of address is configured
- **THEN** the owner receives a formal failure notice ending in ", sir."

#### Scenario: Notice uses the configured address as written
- **WHEN** the form of address is "Mr. Ferreira" and the owner sends a photo
- **THEN** the owner receives a formal notice that only text is supported, ending in ", Mr. Ferreira."

#### Scenario: Partial completion
- **WHEN** an owner turn fails after a tool ran
- **THEN** the notice also says, in the same register, that some actions may have partly completed, and the form of address appears once in the whole notice

#### Scenario: Custom persona keeps the notices formal
- **WHEN** a custom persona is configured
- **THEN** the fixed notices still use the butler register and the configured form of address

#### Scenario: Fallback notice
- **WHEN** the brain `bonsai` fails and alfred goes back to the startup brain `gemma`
- **THEN** the owner receives a formal notice naming `bonsai` and `gemma`, with the form of address once
