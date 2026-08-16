# RESEARCH PROMPT — twscrape XClIdGen breakage (X scout dead since 2026-07-20)

## Symptom
Every X handle fetch in `lumen_scout_expanded.py` returns "0 new". Log shows,
per handle, 3x:
  `XClIdGen creation attempt N/3 failed: Exception: Couldn't get XClientTxId indices script`
then the scout's own (MISLEADING) warning: "not resolvable (cookies expired /
account locked?)".

## Established facts (verified 2026-07-26, do not re-litigate)
- Account pool is HEALTHY: twscrape_accounts.db -> lumen_scout, active=1, error_msg empty.
  The "cookies expired" message is the scout's wrong guess, not the cause.
- Installed twscrape 0.19.1. PyPI latest 0.19.2. UNVERIFIED whether 0.19.2
  postdates the break — check its changelog/upload date FIRST.
- Upstream: github.com/vladkens/twscrape issue #320, opened 2026-07-21, still
  OPEN, no assignee, no linked PR. No upstream fix as of 2026-07-26.
- Upstream root cause: anonymous/logged-out requests are served a web app build
  that no longer contains the script `INDICES_FILE_RE` matches
  (twscrape/xclid.py ~L236). Authenticated sessions DO get it. The indices are
  needed to compute the `x-client-transaction-id` header.
- Our first failure: 2026-07-20 21:01 PDT. Cron runs hourly; ~6,600 failed
  resolutions logged since.
- Blast radius: all 22 TIER8_X handles. Zero X items in seen_items for 14 days.
  Confirmed by Frank manually forwarding two X posts on 2026-07-26 that the
  scout should have caught (@tom_doerr reconya, @DanKornas ComfyUI).

## Question to answer
What is the minimal, maintainable way to restore X ingestion?

Evaluate at least:
1. Upgrade to 0.19.2 — does it change xclid.py? (cheapest if yes)
2. Force the XClIdGen bootstrap through an AUTHENTICATED session rather than
   the logged-out fetch it currently does. Per the issue this is the actual
   asymmetry. Is there a supported hook, or does it need a local patch/fork?
3. Pin/vendor a patched xclid.py with an updated INDICES_FILE_RE.
4. Abandon twscrape for this use case — e.g. RSS bridges (nitter-style),
   or accept X as a manual-only source and drop the cron.

## Constraints
- No paid X API. No new recurring cost.
- Must not require Frank to re-auth by hand on a schedule.
- If patching a site-packages file: vendor it into /home/lumen/minerva and pin
  the version, do NOT edit site-packages in place (silently lost on upgrade).
- Deliver: exact commands, a test that proves >0 items ingested for a known
  handle, and a rollback.

## Also fix regardless of outcome
- `lumen_scout_expanded.py` reports "cookies expired / account locked?" for what
  is actually a library/header failure. Make the message reflect the real error
  so the next person is not sent down the auth rabbit hole.
- Feed-health: the scout ran hourly for 6 days returning zero and nothing
  alerted. Whatever the fix, add a zero-yield alert for this source.
