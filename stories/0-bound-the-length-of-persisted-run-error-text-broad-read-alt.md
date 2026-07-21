# Story

## Title
Bound the length of persisted run error text — broad read

## Slug
`bound-the-length-of-persisted-run-error-text-broad-read-alt`

## Scope
`infra`

## Summary
Bound persisted run error text in `factory.runner._record_run` via a pure truncation helper so oversized already-redacted errors do not bloat persisted run rows, while preserving unchanged storage for in-bound text and proving both paths with unit coverage.

# Acceptance Criteria

- [x] A pure helper truncates a string to a bounded max length (default 4000 chars), appending a clear marker like '...[truncated N chars]' when it cuts.
- [x] `_record_run` applies the bound to the (already-redacted) error before persisting, so no runs row stores an error longer than the bound.
- [x] Text at or under the bound is returned unchanged; the helper is idempotent.
- [x] A unit test covers: an over-long error is truncated with the marker on the persistence path, and a short error is stored verbatim.

# Dev Agent Record

## Status
Done

## Completion Notes
- Fixed `truncate_error` to reconcile marker length with truncated prefix length for very large inputs, guaranteeing the returned value never exceeds the configured bound.
- Preserved unchanged behavior for text at or under the bound and retained idempotence by ensuring bounded outputs are stable on repeated application.
- Verified `_record_run` persists the already-redacted error through the bounded helper before writing the runs row.
- Strengthened persistence-path coverage with a very long error payload to prove bounded storage still holds when truncation count grows to six digits.

## File List
- `factory/runner.py`
- `tests/test_runner_truncate_error.py`
