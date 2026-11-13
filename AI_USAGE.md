# AI Usage Disclosure

## Tools used
Claude Code (Anthropic, Claude Opus 5.5).

## What I used them for
- Writing the implementation (`solution/ledger/*`), the test suite (`solution/tests/*`) and the
  first draft of `SUBMISSION.md`.
- Building an **independent reference implementation** from the README alone, by a separate
  agent that never saw the solution code. Its output was diffed against the pipeline to catch
  misreadings of the spec.
- Timing the large dataset.

## What I wrote or decided myself
_(fill in honestly: which design decisions you reviewed, changed or made. For example: SQLite vs
Postgres, linking at report time, HMAC blind index + Fernet for PII, the rounding convention.)_

## Where the AI was wrong or unhelpful
- The first draft of the end-to-end test expected `events_total = 15` for a fixture that has 16
  non-empty lines. Running the tests showed it, and recounting the fixture by hand confirmed 16.
- The first draft of the atomicity test put the good events and the conflicting batch in the
  same drop. Leads are processed before events, so the test failed before any event was written
  and didn't prove rollback. It was changed to a good drop followed by a bad drop.
- The first event splitter used `str.splitlines()`. That also splits on U+2028 and similar
  characters that can legally appear inside JSON strings. It was changed to split on `\n` only.

## How I verified AI-generated code
- 94 pytest tests: unit tests per rule, end-to-end fixtures with exact expected reports,
  idempotency, incremental vs all-at-once equality, and no plaintext PII in the DB bytes.
- A byte-for-byte diff against an independently written oracle on two seeds and both scales.
- The output matches the two literal examples in the README.
- _(add: reading the code line by line before the interview)_
