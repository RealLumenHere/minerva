#!/usr/bin/env python3
"""wq#1773 — as-of date for research-corpus rows.

Two jobs, both display-only:

  asserted_at(content, mtime)  -> when the doc ASSERTS it was true (front-matter
                                  `date:` if the doc carries one, else file mtime).
                                  Deliberately NOT indexed_at: indexed_at is when
                                  the fleet happened to embed the file, which a
                                  re-index rewrites, so it ages backwards.
  render(asserted, now)        -> "(as of 2026-07-11, 62d)" for the attribution line.

Nothing here touches embeddings, scoring or ordering: a reader that imports this
prints one more span per hit and is otherwise identical.

Corpus reality at build time (2026-09-11, measured over all 10,566 research_docs
rows): 0 docs open with a `---` YAML fence and 15 carry a leading `date:` key, so
asserted_at is mtime for ~99.9% of the corpus today. The parser exists so docs that
DO declare a date are believed, and so future writers have a contract to write to.
"""
from datetime import datetime, timezone, date
import re

__all__ = ["front_matter_date", "asserted_at", "age_days", "render", "attribute",
           "MAX_BACKDATE_DAYS"]

# `date: 2026-07-11`, `date: "2026-07-11"`, `date: 2026-07-11T09:30:00Z`, and -- after
# emphasis markers are stripped -- the `**Date:** 2026-07-11` the corpus's briefs use.
_DATE_KEY = re.compile(r"^date\s*:\s*['\"]?(\d{4}-\d{2}-\d{2})(?:[T ][0-9:.+\-Z]*)?['\"]?\s*$", re.I)
_EMPHASIS = str.maketrans("", "", "*_")
_FENCE = "---"
_SCAN_LINES = 10          # how deep "front matter" can be in an unfenced doc
_MAX_SCAN_CHARS = 4000    # never walk a whole 200KB doc looking for a header

# How far before its own mtime a doc's declared date may sit and still be read as a
# claim about the doc rather than about its source material. See asserted_at().
MAX_BACKDATE_DAYS = 365


def _parse_day(s):
    try:
        y, m, d = (int(x) for x in s.split("-"))
        return datetime(y, m, d, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def front_matter_date(content):
    """The date a doc declares about itself, or None.

    Accepts the fenced YAML form:

        ---
        title: ...
        date: 2026-07-11
        ---

    and the unfenced header-block form the corpus actually uses, where the title is
    followed within the first few lines by a `Date:` / `**Date:**` line:

        # Research: news_moe-models-around-a2b

        **Technical Brief - Mid-Size Mixture-of-Experts**
        Prepared for: Frank - Lumen Research Unit
        **Date:** 2026-08-07

    Only the first _SCAN_LINES lines are eligible, and the line must be a bare date
    key with an ISO date -- prose further down that merely mentions a date is not a
    claim about when the doc was true, so it is ignored.
    """
    if not content:
        return None
    lines = content[:_MAX_SCAN_CHARS].splitlines()
    if not lines:
        return None

    if lines[0].strip() == _FENCE:
        for line in lines[1:]:
            if line.strip() == _FENCE:
                break
            m = _DATE_KEY.match(line.strip().translate(_EMPHASIS).strip())
            if m:
                return _parse_day(m.group(1))
        return None

    for line in lines[:_SCAN_LINES]:
        m = _DATE_KEY.match(line.strip().translate(_EMPHASIS).strip())
        if m:
            return _parse_day(m.group(1))
    return None


def _as_utc(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


def asserted_at(content, mtime, now=None):
    """Front-matter date when the doc declares a believable one, else mtime.

    Two declared dates are not believed, both falling back to mtime:

      * the future -- a doc cannot assert a claim it has not made yet (usual cause:
        a template placeholder or a typo'd year), and a negative age renders as 0d,
        which would read as brand new;
      * more than MAX_BACKDATE_DAYS before mtime -- measured 2026-09-11 over the
        live corpus, 33 of 623 declared dates sit 1-1.5 YEARS before mtime because
        the brief is dating its SOURCE (a 2025 Reddit post, a repo's creation date),
        not itself, and several contradict the 2026 date in their own filename.
        Believing those would over-discount a current brief as a two-year-old one,
        which is the same failure as under-discounting, pointed the other way.
    """
    now = _as_utc(now) or datetime.now(timezone.utc)
    mtime = _as_utc(mtime)
    declared = front_matter_date(content)
    if declared is None or declared > now:
        return mtime
    if mtime is not None and (mtime - declared).days > MAX_BACKDATE_DAYS:
        return mtime
    return declared


def age_days(asserted, now=None):
    """Whole days between `asserted` and now, floored at 0. None if unknown."""
    asserted = _as_utc(asserted)
    if asserted is None:
        return None
    now = _as_utc(now) or datetime.now(timezone.utc)
    return max(0, (now - asserted).days)


def render(asserted, now=None):
    """The attribution span: `(as of 2026-07-11, 62d)`.

    `(as of unknown)` when a row predates the column and has not been backfilled --
    an unlabelled hit is exactly the failure this ticket exists to remove, so an
    unknown date is stated rather than silently omitted.
    """
    asserted = _as_utc(asserted)
    if asserted is None:
        return "(as of unknown)"
    return f"(as of {asserted:%Y-%m-%d}, {age_days(asserted, now)}d)"


def attribute(line, asserted, now=None):
    """Append the as-of span to an attribution line."""
    return f"{line}  {render(asserted, now)}"


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        print(render(_parse_day(arg)))
