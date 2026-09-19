# Current goal and handoff

## Goal

Make the one-button portfolio briefing responsive and understandable even when
Apify is slow or unavailable. Preserve all financial calculations, missing-data
semantics, and strict article relevance checks.

## Implemented on 19 September 2026

- News collection stops waiting after a shared 60-second budget.
- The app runs at most two queries concurrently. Standalone collector callers
  retain one worker by default.
- Completed query results survive other failures or timeouts. Coverage is marked
  partial, or unavailable when all queries fail; empty successful searches remain
  distinct from failures. Progress reports completed queries and deadline expiry.
- Apify queries use a maximum 28-second local budget and remote actor timeout.
  HTTP waits are bounded by the remaining budget, up to 15 seconds per request.
  Poll failures attempt a two-second abort. Paid actor creation is never retried.
- Successful news outcomes, including successful empty searches, are cached for
  30 minutes in the user's Streamlit session. The key includes the case path,
  source/reference fingerprint, client and portfolio. Maximum 32 entries.
  Failed or partial results are not cached. Original news timestamps are preserved.
- OpenAI clients use a 45-second request timeout and no automatic retries.
  Narrative failures retain the computed analysis and other successful narratives.
- UI uses consistent typography, Markdown emphasis instructions, common LaTeX
  cleanup, responsive columns, and distinct news-status messages.

## Important limits

The 60-second budget covers news waiting, not the whole workflow. Up to three
AI narratives still run sequentially. SDK timeouts bound network waits rather than
providing a strict wall-clock deadline for the entire generation stage.

Python cannot forcibly stop an already running worker thread. The app stops
waiting; the live adapter has its own timeouts and attempts remote cancellation.
Abort is best effort. If actor creation succeeded remotely but its response was
lost, no run ID is available to abort; the remote runtime limit remains the fallback.

## Verification

233 offline tests passed, including stalled-provider deadline handling, partial
batch preservation, abort without duplicate paid creation, cache expiry and
case isolation, plus the Streamlit one-click/rerun test. No paid live API requests
were made. Relevant modified news/UI/workflow files passed Ruff checks.

## Useful next improvements

1. Show the calculated portfolio immediately while news and narratives continue.
2. Add a user-visible refresh-news action that bypasses the cache.
3. Add an overall generation deadline and stream narrative output.
4. Measure per-stage latency in a live run before tuning budgets further.
5. Improve fund-name search queries separately, preserving verifiable holding
   matches and date checks. Fast searches do not guarantee relevant articles.

## Working preferences

Do not capture screenshots. Do not merge or push without authorization. Do not
expose API keys. Test with fake providers before making paid requests. Current
project is this directory, not older copies in Downloads.
