# googleapis-without-429

[![PyPI](https://img.shields.io/pypi/v/googleapis-without-429)](https://pypi.org/project/googleapis-without-429/)
[![Python](https://img.shields.io/pypi/pyversions/googleapis-without-429)](https://pypi.org/project/googleapis-without-429/)
[![CI](https://github.com/pavlosambur/googleapis-without-429/actions/workflows/ci.yml/badge.svg)](https://github.com/pavlosambur/googleapis-without-429/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Stay inside Google API quotas instead of recovering from `429 Too many
requests`. One argument, and the rest of your code is unchanged.

```python
import gspread
from googleapis_without_429 import RateLimitedSession

session = RateLimitedSession(credentials)  # <- the only change
gc = gspread.authorize(credentials, session=session)

sheet = gc.open("My Sheet").sheet1
for row in rows:
    sheet.append_row(row)  # waits when the quota is spent, then continues
```

No decorators to add, no calls to rewrite, no `sleep()` sprinkled through the
loop. The session knows what Google's quotas are and paces itself.

## Install

```bash
pip install googleapis-without-429
```

Requires Python 3.10 or newer.

## The problem

Google's per-minute quotas are small. Sheets allows **60 reads and 60 writes
per minute per user** — a loop that appends rows hits that in a minute of
ordinary work, and the script dies partway through with half the data written.

The official advice is exponential backoff, and every retry library implements
it. But backoff is a reaction *after* the failure: it recovers, it does not
prevent. The better first move is not to exceed the quota at all, and to keep
retries as the second line of defence.

That is what this does. It tracks what you have spent against a sliding window
and blocks the call that would go over, instead of letting Google reject it.

## What is covered

| API | Quota | How it is metered |
|---|---|---|
| Sheets | 60 reads + 60 writes per minute | separate buckets; every call costs 1, batches included |
| Drive | 325,000 quota units per minute | one shared bucket; a call costs 5 to 200 units |

Together these cover [gspread](https://github.com/burnash/gspread) completely —
which needs both, since Sheets moves the cell data while Drive owns the file:

| gspread call | Goes to |
|---|---|
| `open_by_key`, `open_by_url` | Sheets |
| `worksheet.get`, `get_all_values`, `batch_get` | Sheets |
| `append_row`, `update`, `clear`, `batch_update` | Sheets |
| `open("title")`, `openall`, `list_spreadsheet_files` | Drive, then Sheets |
| `create`, `copy`, `del_spreadsheet`, `share` | Drive |

Nothing else is covered yet: Gmail, Calendar and Docs have no profile, and
`google-api-python-client` uses a different transport. Both are on the roadmap.
A request to any host without a profile passes through untouched — including
the token refresh your credentials perform, which must not eat the quota of the
API you are actually calling.

## This does not remove the need for retries

It reduces 429s. It does not eliminate them, and any library claiming otherwise
is overselling.

The reason is that the two sides count differently. This library slides a
window over the timestamps of *your* calls. Google meters *fixed* windows whose
boundaries you cannot see. So 60 calls that look perfectly spaced from here can
land as 30 in the tail of one of Google's minutes and 30 in the head of the
next — and the second batch is over the limit even though our counter says
there is room.

Being strict about our own window makes us conservative, never reckless: we may
allow fewer calls than Google would, never more. But the boundary mismatch is
real, so a retry on 429 is built in and on by default:

```python
RateLimitedSession(
    credentials,
    max_attempts=5,  # total tries per request, including the first
    backoff_base=1.0,  # ceiling for the first retry delay, in seconds
    backoff_cap=60.0,  # the ceiling stops doubling here
)
```

Delays use equal jitter: half the ceiling is always waited and the rest is
randomised. The guaranteed half matters — a 429 means the window has not
reopened yet, so a delay that comes out near zero only buys another 429. A
`Retry-After` header, if the server sends one, wins over the computed delay.

## Adjusting the limits

The shipped numbers are Google's documented defaults, and defaults go stale.
Real quotas depend on the project, on when it was created, and Google revises
them — Drive's changed on 1 May 2026, and projects already using the API kept
the previous ones. So overriding is a first-class operation:

```python
from googleapis_without_429 import DRIVE, SHEETS, RateLimitedSession

session = RateLimitedSession(
    credentials,
    [
        SHEETS.with_limits(read=300, write=300),  # the per-project ceiling
        DRIVE.with_limits(units=12_000),  # an older project
    ],
)
```

The defaults are the **per-user** quotas, which is what a single script runs
into. Raise them to the per-project ceiling only if the job really is the only
thing using that project. A misspelled bucket name raises rather than being
quietly ignored, so a typo cannot leave you believing a limit was raised.

Check what your project actually has in the Cloud Console under
**APIs & Services → Quotas**; it can differ from the documentation.

## Without a session

If the calls are not made through a `requests` session — a hand-rolled client,
a worker, an API this library has no adapter for — use the limiter directly. It
is both a context manager and a decorator:

```python
from googleapis_without_429 import SHEETS, QuotaLimiter

limiter = QuotaLimiter([SHEETS])


@limiter.limit(SHEETS, "write")
def push_batch(rows): ...


with limiter.limit(SHEETS, "read"):
    ...
```

And the raw window underneath, when nothing above fits:

```python
limiter.bucket(SHEETS, "write").acquire(cost=1)
limiter.bucket(SHEETS, "write").used  # what is currently counted
```

A session exposes its own limiter the same way, so you can pace a call it does
not make itself:

```python
session.limiter.bucket(SHEETS, "read").acquire()
```

## Adding an API

A profile is data, not code: a host, a map of buckets to limits, and a function
that says which bucket a call belongs to and what it costs.

```python
from googleapis_without_429 import ApiProfile, QuotaLimiter


def resolve_docs(http_method, path, query):
    return ("read" if http_method == "GET" else "write"), 1


DOCS = ApiProfile(
    name="docs",
    host="docs.googleapis.com",
    limits={"read": 300, "write": 60},
    resolve=resolve_docs,
)

limiter = QuotaLimiter([DOCS])
```

Two things worth knowing before writing one:

- **The HTTP method is not the whole story.** Sheets sends several reads as
  POST (`:getByDataFilter`, `:batchGetByDataFilter`, `developerMetadata:search`)
  because they carry a request body. Charging those to the write bucket burns
  one of only 60 writes a minute. The published
  [discovery document](https://developers.google.com/discovery/v1/reference)
  for an API lists every method with its real HTTP verb and path.
- **A host may serve several APIs.** Drive lives on `www.googleapis.com`
  alongside others, so its profile claims `path_prefixes=("/drive/",
  "/upload/drive/")`. Leave that empty only when the host belongs to one API.

Profiles for other Google APIs are very welcome as pull requests — that is the
cheapest way for this library to grow, and it needs no changes to the core.

## Development

```bash
uv sync                      # install the project and its dev dependencies
make check                   # everything CI runs: lint, format, types, tests
uv run pre-commit install    # optional: fast checks on every commit
```

Individual steps: `make lint`, `make check-format`, `make typecheck`,
`make test`. CI calls the same targets, so a green local run means a green
pipeline. The test suite needs no credentials and makes no network calls.

## Roadmap

- An adapter for `google-api-python-client`, which uses an httplib2-style
  transport rather than a `requests` session
- A Gmail profile — its quota units range from 2 to 100 per call, which is what
  the weighted core was built for
- Async support

## License

MIT
