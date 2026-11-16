# Spec Delta

## ADDED Requirements

### Requirement: Per-user confirm list
Each user SHALL have a confirm list in the allow-list syntax, empty by default, naming the tools whose calls on that user's turns wait for that user's confirmation (see `user-input-requests`). One user's confirm list SHALL NOT affect another user's turns. Entries SHALL follow the allow-list prefix rules: an entry naming an alfred tool that does not exist SHALL be a startup error, and an entry the user's allow-list can never match SHALL be logged as a warning at startup.

#### Scenario: Per-user trust
- **WHEN** `ana` has `confirm = ["mcparr_add_media"]` and `diogo` has none
- **THEN** `ana` confirms each film added on her turns, and `diogo`'s turns add films at once

#### Scenario: Entry the allow-list hides
- **WHEN** `ana` has `confirm = ["cookbook_*"]` and her allow-list has no `cookbook_` entry
- **THEN** alfred starts and logs a warning naming `ana` and the entry
