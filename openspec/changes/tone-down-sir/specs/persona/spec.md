# Spec Delta

## MODIFIED Requirements

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
