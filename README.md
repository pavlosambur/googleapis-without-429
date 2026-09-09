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

Nothing else is covered yet: Gmail, Calendar and Docs have no profile. A
request to any host without a profile passes through untouched — including
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
from googleapis_without_429 import RateLimitedSession, RetryPolicy

RateLimitedSession(
    credentials,
    retry=RetryPolicy(
        max_attempts=5,  # total tries per request, including the first
        backoff_base=1.0,  # ceiling for the first retry delay, in seconds
        backoff_cap=60.0,  # the ceiling stops doubling here
    ),
)
```

Delays use equal jitter: half the ceiling is always waited and the rest is
randomised. The guaranteed half matters — a 429 means the window has not
reopened yet, so a delay that comes out near zero only buys another 429. A
`Retry-After` header, if the server sends one, wins over the computed delay.

### Drive answers 403, not 429

Google is not consistent here, and it matters. Sheets returns `429` when you go
too fast. **Drive returns `403 Forbidden`** for the same condition, and only
sometimes 429 — so a retry that watches for 429 alone quietly does nothing on
exactly the calls it was meant to protect.

A 403 is also the ordinary answer for *you may not touch this file*, so the
status code alone cannot decide. The reason string in the response body can:

| Response | Retried | Why |
|---|---|---|
| `429` | yes | unambiguous |
| `403` + `rateLimitExceeded` | yes | clears within the minute |
| `403` + `userRateLimitExceeded` | yes | clears within the minute |
| `403` + `dailyLimitExceeded` | **no** | resets at midnight Pacific; retrying achieves nothing |
| `403` + `sharingRateLimitExceeded` | **no** | measured over far too long a period |
| `403`, anything else | **no** | a permission error — retrying turns a clear failure into a slow one |

A body that is missing, not JSON, or shaped unexpectedly is treated as *not* a
rate limit, so a malformed response can never turn into a retry loop.

### Server errors, and the write you do not want twice

Google's guidance also recommends backoff for `500`, `502`, `503` and `504`,
and those are retried too — but **only for methods that are safe to repeat**.

A 5xx means the server may have applied your change and then failed to answer.
Repeating a `GET` costs nothing; repeating `values:append` adds the row twice,
and a duplicated row is a worse outcome than an error you can see. So `GET`,
`HEAD`, `OPTIONS`, `PUT` and `DELETE` are retried on 5xx, and `POST` is not.

Rate limits are different: a 429 or a rate-limit 403 means the request was
*rejected*, not half-applied, so those are retried whatever the method.

If your POSTs genuinely are safe to repeat, say so:

```python
from googleapis_without_429 import RetryPolicy

RetryPolicy(retry_unsafe_server_errors=True)
```

Or switch server-error retries off entirely with
`RetryPolicy(retry_server_errors=False)`.

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

### When the metering model itself differs

`with_limits` changes a number. Sometimes the whole model is different: Drive
counted **requests** before 1 May 2026 and counts **weighted quota units**
after, and a project that was already using the API kept the old scheme. There
is no conversion between the two, so no single number bridges them — such a
project needs its own profile, with its own `resolve` and its own window:

```python
from googleapis_without_429 import ApiProfile, RateLimitedSession

DRIVE_LEGACY = ApiProfile(
    name="drive",
    host="www.googleapis.com",
    path_prefixes=("/drive/", "/upload/drive/"),
    limits={"queries": 12_000},  # your project's real figure, from the Console
    resolve=lambda method, path, query: ("queries", 1),  # requests, not units
    window=100.0,  # some legacy quotas are metered per 100 seconds
)

session = RateLimitedSession(credentials, [DRIVE_LEGACY])
```

The window belongs to the profile, not to the session, so a limiter can hold a
per-minute quota and a per-100-seconds one at the same time.

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

## google-api-python-client

The official client does not take a `requests` session — it takes an
httplib2-style transport — so it gets an adapter of its own:

```python
import google_auth_httplib2
import httplib2
from googleapiclient.discovery import build

from googleapis_without_429 import RateLimitedHttp

authorised = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http())
service = build("sheets", "v4", http=RateLimitedHttp(authorised))

service.spreadsheets().values().append(
    spreadsheetId=sheet_id, range="A1", body={"values": rows}
).execute()
```

Same profiles, same quotas, same retry rules. `httplib2` is not a dependency of
this library: the adapter wraps whatever transport you hand it.

If a program uses both clients, give them one limiter so they share a quota
instead of each keeping its own:

```python
from googleapis_without_429 import QuotaLimiter, RateLimitedHttp, RateLimitedSession

limiter = QuotaLimiter()
session = RateLimitedSession(credentials, limiter=limiter)
http = RateLimitedHttp(authorised_http, limiter=limiter)
```

## Failing instead of waiting

By default the limiter waits as long as the quota needs, which is right for a
batch job and wrong for anything serving a request. A web handler that stalls
for fifty seconds is indistinguishable from a hung process, and the caller
would almost always rather have an error:

```python
from googleapis_without_429 import QuotaTimeoutError, RateLimitedSession

session = RateLimitedSession(credentials, acquire_timeout=5.0)

try:
    session.get(url)
except QuotaTimeoutError as exc:
    print(f"gave up after {exc.waited:.1f}s waiting for {exc.cost} unit(s)")
```

`QuotaTimeoutError` subclasses the built-in `TimeoutError`, so code that
already handles timeouts catches it without knowing this library exists. No
quota is consumed when it raises.

For work that can simply be skipped, ask instead of waiting:

```python
from googleapis_without_429 import SHEETS, QuotaLimiter

limiter = QuotaLimiter()


def refresh(spreadsheet_id):
    if limiter.try_acquire(SHEETS, "read"):
        return fetch_now(spreadsheet_id)
    return serve_cached(spreadsheet_id)
```

## Seeing what it is doing

A rate limiter that is working correctly looks exactly like a program that has
hung. Every bucket therefore counts what it has done, with no configuration:

```python
for name, stats in session.limiter.stats().items():
    print(
        f"{name}: {stats.granted} granted, {stats.waits} waited "
        f"{stats.wait_seconds:.1f}s total, {stats.timeouts} timed out"
    )
```

```
sheets:read: 240 granted, 3 waited 58.2s total, 0 timed out
sheets:write: 60 granted, 0 waited 0.0s total, 0 timed out
drive:units: 4 granted, 0 waited 0.0s total, 0 timed out
```

Read that as a diagnosis: reads are the bottleneck and writes are nowhere near
their limit, so raising the read limit — if the project's real quota allows —
is what would speed this job up.

Waiting is logged at `DEBUG` and retries at `INFO`, under the
`googleapis_without_429` logger:

```python
import logging

logging.getLogger("googleapis_without_429").setLevel(logging.DEBUG)
```

```
DEBUG googleapis_without_429.core: sheets:read: quota exhausted, waiting 12.480s for 1 unit(s)
INFO  googleapis_without_429.session: GET /v4/spreadsheets/abc: rate limited (429), retrying in 1.42s (attempt 2 of 5)
```

## Fairness

Callers are served in the order they arrived. That matters as soon as calls
cost different amounts: Drive charges 200 units for a download and 5 for a
metadata read, so without an order the cheap calls keep the window just full
enough that the expensive one never fits — and it waits forever while
everything around it proceeds.

The queue costs a little throughput, since a cheap call that would fit right
now waits behind an expensive one that does not. That is the trade being made
deliberately: a call that never runs is a worse outcome than one that runs
slightly later. For Sheets it changes nothing at all, because every call there
costs exactly one.

## Threads

The limiter is thread-safe. `WeightedSlidingWindow` and `QuotaLimiter` are
built for concurrent use and the test suite exercises them on the free-threaded
build (3.14t) in CI, where threads really do run at the same instant.

The session is a different matter: it inherits from `requests.Session`, whose
documentation makes no thread-safety promise either way. The reliable shape is
therefore **one session per thread, sharing a single limiter** — otherwise each
session keeps its own quota and the effective limit is multiplied by the number
of threads, which is how you hit 429 while believing you are being careful:

```python
from concurrent.futures import ThreadPoolExecutor

from googleapis_without_429 import QuotaLimiter, RateLimitedSession

limiter = QuotaLimiter()  # one set of buckets for the whole program


def fetch(spreadsheet_id):
    session = RateLimitedSession(credentials, limiter=limiter)
    return session.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    )


with ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(fetch, spreadsheet_ids))
```

Every worker waits on the same quota, so eight threads consume the same 60
reads a minute that one thread would.

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

- A Gmail profile — its quota units range from 2 to 100 per call, which is what
  the weighted core was built for
- Async support

## License

MIT
