# Project charter

> **Maintainers: replace this file with your project's charter.** Every agent
> reads it before working, so the clearer it is, the better the fleet's work.
> Keep it short and concrete.

## Mission

What this project is for, in two or three sentences, and who it serves.

## Scope

- In scope: …
- Out of scope (don't build these, even if a task seems to ask): …

## Architecture

The main parts of the code and how they fit together. Where new code should go.

## Conventions

- Language versions, style and formatting tools.
- How errors, logging and configuration are handled.
- Naming, file layout, commit message style.

## Checks

How to prove a change works (this is also `validate.command` in `workstead.json`):

```bash
# e.g. npm ci && npm test
```

## Definition of done

A task is done when its acceptance criteria are met, the checks pass, new
behaviour has tests, and user-facing changes are documented.

## Decisions

Choices already made that agents shouldn't relitigate (with a line on why).
