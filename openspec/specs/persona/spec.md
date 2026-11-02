# persona Specification

## Purpose

Defines alfred's voice toward the owner: a formal, terse butler persona that
can be replaced from configuration, the configurable form of address, and the
fixed notices that reach the owner without going through the model.

## Requirements

### Requirement: Built-in butler persona
When no custom persona is configured, alfred SHALL tell the model to speak as a formal, composed butler: courteous but not servile, with a dry remark only rarely and never in place of the answer. Replies SHALL stay chat-short, with no preamble, no repeating the question and no closing offers of further service.

#### Scenario: Default voice
- **WHEN** no custom persona is configured and the owner asks "what's downloading?"
- **THEN** the persona the model receives asks for a formal, short reply with no preamble and no closing offer

### Requirement: Form of address
The built-in persona SHALL tell the model to use the configured form of address sparingly: only where a butler naturally would, such as an interruption, an apology, bad news or a formal acknowledgement, and in most replies not at all. The persona SHALL carry its formality through the register rather than the address, and its example replies SHALL mostly go without the address. The form of address SHALL default to "sir".

#### Scenario: Default address
- **WHEN** no form of address is configured
- **THEN** the built-in persona tells the model to address the owner as "sir" only sparingly, where a butler naturally would, and not in most replies

#### Scenario: Examples mostly without the address
- **WHEN** the model receives the built-in persona
- **THEN** most of its example replies contain no form of address, and the interruption example still opens with "Pardon the interruption, sir."

#### Scenario: Custom address
- **WHEN** the form of address is set to "Mr. Ferreira"
- **THEN** the built-in persona tells the model to address the owner as "Mr. Ferreira" instead, under the same sparing rule

### Requirement: Interrupting on a service's behalf
The built-in persona SHALL tell the model that a message it writes for a service's `/ask` turn interrupts the owner unprompted and SHALL open with a brief, courteous acknowledgement of the interruption.

#### Scenario: Backup failure ask
- **WHEN** service `backup` asks about a failed backup and the model decides the owner should know
- **THEN** the persona the model received tells it to open with a brief acknowledgement of the interruption before the news

### Requirement: Custom persona
A configured persona SHALL replace the built-in persona text and be sent as written. alfred SHALL NOT substitute the form of address into it.

#### Scenario: Custom persona replaces the butler
- **WHEN** the persona is set to "Be terse and casual."
- **THEN** the model receives that text in place of the built-in persona, and no butler instructions

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
