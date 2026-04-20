# OpenRouter free-tier roster tracker

*Shipped in v0.5-D (2026-04-20). Runner:
[`scripts/openrouter_roster_diff.py`](../../scripts/openrouter_roster_diff.py).
Tests: [`tests/test_openrouter_roster_diff.py`](../../tests/test_openrouter_roster_diff.py).*

## Why this exists

The v0.4-B retrospective surfaced a concrete failure mode of the
"free model roster" that the OpenRouter provider depends on:
`deepseek-r1:free` had been silently withdrawn by the time we looked,
and the audit that was supposed to catch the removal was itself
triggered only because a user had hit a 404. Reactive, by definition.

This tracker is the proactive counterpart. It's a single-file cron
that polls [`/api/v1/models`](https://openrouter.ai/api/v1/models)
once a week, filters to the free tier, and records any delta against
the previously committed snapshot. The primary audit trail is
[`CHANGES.md`](./CHANGES.md) — append-only markdown, prepended
newest-first, so `git log -p CHANGES.md` tells the story of
free-tier churn at a glance.

## What makes something "free"

The script reads the `pricing` object from the OpenRouter response
and treats an entry as free **iff** both `prompt` and `completion`
parse as numeric zero. The `:free` suffix convention in model IDs
is *not* used — OpenRouter occasionally lists a `:free` variant
with a nonzero completion price (observed historically on some
preview models). Pricing is authoritative; the suffix is a hint.

`test_is_free_does_not_require_free_suffix` pins this invariant.

## Design boundaries

- **Zero coupling to the `coderouter` package.** The script imports
  only `stdlib + httpx`. It does not load `providers.yaml`, touch any
  adapter, or know about profiles. That keeps the cron runnable even
  when the main package is mid-change, and makes it safe to invoke
  from any environment that has `httpx` installed — no editable
  install required.
- **Human-readable diff log.** `CHANGES.md` is markdown, not JSON.
  The diff format is deliberately grep-friendly: `⚠️ REMOVED` at the
  top of each entry so a quick `grep REMOVED CHANGES.md` surfaces
  the cases that usually motivate a response (user-visible failures
  come from removals, not additions).
- **First-run baseline is silent.** If `latest.json` is absent, the
  script writes it and exits 0 *without* appending to `CHANGES.md`.
  A hundred-line "Added:" block on the first run would be noise that
  dilutes the signal of actual churn. Tracking starts at run 2.

## Files in this directory

| File | Purpose | Written by |
|---|---|---|
| `README.md` | This document — runbook + design notes. | human |
| `CHANGES.md` | Append-only log (prepended newest-first) of free-tier churn. | script |
| `latest.json` | Last observed roster snapshot. Rewritten atomically every run. | script |

`latest.json` is tracked in git: the snapshot IS the baseline, and
a committed snapshot is what lets `CHANGES.md` entries be reviewed
in the same PR as the data that produced them.

## Runbook

### Manual

```bash
# Normal invocation — fetch, diff against latest.json, update files.
python scripts/openrouter_roster_diff.py

# Dry run — fetch + diff, print summary, write nothing.
python scripts/openrouter_roster_diff.py --dry-run

# Custom paths (mostly for tests; defaults are the ones above).
python scripts/openrouter_roster_diff.py \
  --snapshot docs/openrouter-roster/latest.json \
  --changes docs/openrouter-roster/CHANGES.md
```

Exit codes:
- `0`: success. Diff may be empty — that's a normal no-op.
- `2`: HTTP fetch failed (transport error, 4xx, 5xx). The script
  prints the error to stderr; `latest.json` and `CHANGES.md` are
  NOT modified.

### Scheduled

Recommended cadence: **weekly**, on a weekday morning (JST). Each
run takes <1 second; OpenRouter's `/api/v1/models` endpoint is
unauthenticated for the roster, so no key management is needed.

When run under the Claude Cowork `schedule` skill, point the
scheduled task at this script with no arguments and review the
resulting PR/commit each time it lands. Manual run is equally
fine — the whole script is idempotent w.r.t. back-to-back calls
(the only effect of a no-op re-run is a refreshed `fetched_at`
timestamp in `latest.json`).

### After a run

1. `git diff docs/openrouter-roster/` — look at what changed.
2. If `CHANGES.md` has a new entry (prepended at the top), read it.
   `⚠️ REMOVED` entries usually warrant action:
   - Update `examples/providers.yaml` if the removed model was
     referenced in any profile.
   - Cross-check [`docs/openrouter-roster.md`](../openrouter-roster.md)
     — the v0.4-B audit document — and either move the entry to its
     "dropped" table or note it inline.
   - Grep the code / tests / `README.md` for the model id and
     update anything that mentioned it as an example.
3. `git commit docs/openrouter-roster/` with a message like
   `roster: weekly sync YYYY-MM-DD` (or a more specific message if a
   removal triggered downstream updates).

### Triage cheatsheet

| Symptom | Probable cause | Action |
|---|---|---|
| `[roster-diff] fetch failed:` on stderr, exit 2 | OpenRouter transient (5xx) or DNS flake | Re-run in a few minutes. If persistent, check [OpenRouter status](https://status.openrouter.ai). |
| No changes logged but roster "feels different" | Pricing string normalized server-side (`0` → `0.0`) or context length unchanged | Inspect `git diff docs/openrouter-roster/latest.json` — pricing/context deltas DO get logged; if nothing shows, there truly was no change. |
| `CHANGES.md` prepends twice in the same day | Script run twice | Squash the second prepend with `git reset` — it's safe, the snapshot is self-describing. |

## Scope (what this tracker does NOT do)

- Does not alert external systems. The only output is files in git.
- Does not cross-reference with other providers. Groq, Cerebras, and
  Anthropic-direct have their own availability surfaces; this tracker
  is OpenRouter-specific because OpenRouter's free tier is the only
  one that rotates frequently enough to need auto-tracking.
- Does not track paid-tier changes. `filter_free` drops everything
  else before the diff runs. The paid catalog changes too often to
  make a useful log.

## Future extensions

If the tracker ever feels thin, the cheapest upgrade is probably:

- **Streaming-capability flag.** The OpenRouter payload includes
  supported transport info per model; tracking that alongside
  pricing would let us notice when `openai/gpt-oss-120b:free`
  loses/gains SSE (has happened at least once during v0.5
  development, inferred from a change in behavior we couldn't
  immediately attribute).
- **Rate-limit band tracking.** The payload hints at free-tier rate
  bands; logging when a model moves between bands is a cheap early
  warning for chain-uniform-429 situations.

Neither is urgent as of v0.5-D. The minimum viable cron — id /
pricing / context — is what the roster currently needs.
