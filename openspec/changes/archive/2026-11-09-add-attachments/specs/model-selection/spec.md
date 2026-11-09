# Spec Delta

## MODIFIED Requirements

### Requirement: Configured brains
The brains SHALL be the `[ai.models]` aliases, each naming a model tag, plus the `[ai] model` tag under its own tag as name when no alias names it. The startup brain SHALL be the one whose tag is `[ai] model`. Each brain SHALL have a vision flag: the alias's `vision` setting, or `[ai] vision` for the `[ai] model` brain when no alias names it, and `false` when not set.

#### Scenario: Startup model has an alias
- **WHEN** `[ai] model = "gemma4:26b-a4b-it-qat"` and `[ai.models]` has `gemma = "gemma4:26b-a4b-it-qat"` and `bonsai = "bonsai:8b"`
- **THEN** the brains are `gemma` and `bonsai`, and alfred starts on `gemma`

#### Scenario: Startup model without an alias
- **WHEN** `[ai] model = "qwen3:14b"` and `[ai.models]` has only `bonsai = "bonsai:8b"`
- **THEN** the brains are `qwen3:14b` and `bonsai`, and alfred starts on `qwen3:14b`

#### Scenario: No list
- **WHEN** the file has no `[ai.models]`
- **THEN** the only brain is the `[ai] model` tag

#### Scenario: Vision declared
- **WHEN** `[ai.models]` has `gemma = { model = "gemma4:26b-a4b-it-qat", vision = true }` and `bonsai = "bonsai:8b"`
- **THEN** `gemma` has vision and `bonsai` does not

#### Scenario: Startup model without an alias declares vision
- **WHEN** `[ai] model = "qwen3:14b"`, `[ai] vision = true` and no alias names `qwen3:14b`
- **THEN** the `qwen3:14b` brain has vision

## ADDED Requirements

### Requirement: Brains without vision get no images
A turn on a brain without vision SHALL NOT send images to the model. Attachments SHALL reach it as handles marked as not viewable by this brain, and `alfred_view_attachment` SHALL NOT be offered. `alfred_read_document` SHALL still be offered.

#### Scenario: Photo on a text-only brain
- **WHEN** alfred is on a brain without vision and the owner sends a photo captioned "what is this?"
- **THEN** the model receives the caption and the photo's handle marked as not viewable, no image, and no `alfred_view_attachment` tool

#### Scenario: PDF on a text-only brain
- **WHEN** alfred is on a brain without vision and the owner sends a PDF whose page 1 has text
- **THEN** the model receives the handle and page 1's text, and can call `alfred_read_document`

### Requirement: Fallback to a brain without vision
If a turn falls back to the startup brain and it has no vision, the run on the startup brain SHALL send no images, and a call to `alfred_view_attachment` in it SHALL return an error saying the current brain can't see images.

#### Scenario: Fallback mid-turn
- **WHEN** a turn on a vision brain falls back to a startup brain without vision, and the model then calls `alfred_view_attachment`
- **THEN** the call returns an error saying the current brain can't see images
