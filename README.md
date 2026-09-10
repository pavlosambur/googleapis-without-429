<p align="center">
  <img src="https://raw.githubusercontent.com/pavlosambur/googleapis-without-429/main/docs/logo.svg"
       alt="" width="76" height="76">
</p>

<h1 align="center">googleapis-without-429</h1>

<p align="center">
  <a href="https://pypi.org/project/googleapis-without-429/"><img alt="PyPI" src="https://img.shields.io/pypi/v/googleapis-without-429"></a>
  <a href="https://pypi.org/project/googleapis-without-429/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/googleapis-without-429"></a>
  <a href="https://github.com/pavlosambur/googleapis-without-429/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/pavlosambur/googleapis-without-429/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

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

The session carries a quota profile for each Google API it knows, and blocks a
call that would exceed one.

## Install

```bash
pip install googleapis-without-429
```

Requires Python 3.10 or newer.

## Contents

- [The problem](https://github.com/pavlosambur/googleapis-without-429#the-problem)
- [Supported APIs and quotas](https://github.com/pavlosambur/googleapis-without-429#supported-apis-and-quotas)
  - [Gmail is priced per method](https://github.com/pavlosambur/googleapis-without-429#gmail-is-priced-per-method)
- [Clients](https://github.com/pavlosambur/googleapis-without-429#clients)
  - [gspread](https://github.com/pavlosambur/googleapis-without-429#gspread)
  - [google-api-python-client](https://github.com/pavlosambur/googleapis-without-429#google-api-python-client)
  - [aiogoogle (async)](https://github.com/pavlosambur/googleapis-without-429#aiogoogle-async)
  - [Any other client](https://github.com/pavlosambur/googleapis-without-429#any-other-client)
  - [Sharing one limiter](https://github.com/pavlosambur/googleapis-without-429#sharing-one-limiter)
- [Retries](https://github.com/pavlosambur/googleapis-without-429#retries)
  - [Drive answers 403, not 429](https://github.com/pavlosambur/googleapis-without-429#drive-answers-403-not-429)
  - [Server errors and writes that must not repeat](https://github.com/pavlosambur/googleapis-without-429#server-errors-and-writes-that-must-not-repeat)
- [Waiting, timeouts and skipping](https://github.com/pavlosambur/googleapis-without-429#waiting-timeouts-and-skipping)
- [Stats and logging](https://github.com/pavlosambur/googleapis-without-429#stats-and-logging)
- [Fairness](https://github.com/pavlosambur/googleapis-without-429#fairness)
- [Threads](https://github.com/pavlosambur/googleapis-without-429#threads)
- [Adjusting the limits](https://github.com/pavlosambur/googleapis-without-429#adjusting-the-limits)
  - [When the metering model differs](https://github.com/pavlosambur/googleapis-without-429#when-the-metering-model-differs)
- [Adding an API](https://github.com/pavlosambur/googleapis-without-429#adding-an-api)
- [Development](https://github.com/pavlosambur/googleapis-without-429#development)
- [Roadmap](https://github.com/pavlosambur/googleapis-without-429#roadmap)

## The problem

Google's per-minute quotas are small. Sheets allows **60 reads and 60 writes
per minute per user**, so a loop appending 60 rows reaches the write limit and
the remaining calls fail with `429`.

The documented remedy is exponential backoff, which acts after the failure.
This library acts before it: each call is counted against a sliding window, and
a call that would exceed the quota blocks until the window has room.

## Supported APIs and quotas

| API | Quota per minute | Metering |
|---|---|---|
| Sheets | 60 reads + 60 writes | separate buckets; every call costs 1, batches included |
| Drive | 325,000 quota units | one shared bucket; a call costs 5 to 200 units |
| Gmail | 6,000 quota units | one shared bucket; a call costs 1 to 100 units |

Calendar and Docs have no profile yet. A request to a host that no profile
claims passes through unmetered, including credential token refresh, which is
therefore never charged against a bucket.

### Gmail is priced per method

Gmail meters per method rather than per call, and the cost is not derivable
from the URL:

| Call | Units |
|---|---|
| `labels.get` | 1 |
| `messages.list` | 5 |
| `messages.get` | **20** |
| `threads.get` | **40** |
| `messages.send` | **100** |

The Gmail profile therefore carries an explicit table of the 63 methods Google
publishes a price for, assembled from the usage-limits page (costs per method
name) and the discovery document (path per method name). The remaining 16
methods — the `settings.cse` and S/MIME families — have no published price and
are charged 100 units, the highest documented cost, so an unrecognised call is
over-counted rather than under-counted.

Sixty `messages.send` calls (60 × 100 = 6,000) exhaust the per-user minute; the
sixty-first waits.

## Clients

| Your client | What you use | Pass it as |
|---|---|---|
| gspread | `RateLimitedSession` | `gspread.authorize(creds, session=...)` |
| google-api-python-client | `RateLimitedHttp` | `build(..., http=...)` |
| aiogoogle | `rate_limited_session(...)` | `Aiogoogle(session_factory=...)` |
| anything else | `QuotaLimiter` | context manager, decorator or direct call |

### gspread

`RateLimitedSession` is a `requests` session, which is what
[gspread](https://github.com/burnash/gspread) accepts — see the example at the
top of this page.

gspread reaches two APIs, and both are metered by default:

| gspread call | Goes to |
|---|---|
| `open_by_key`, `open_by_url` | Sheets |
| `worksheet.get`, `get_all_values`, `batch_get` | Sheets |
| `append_row`, `update`, `clear`, `batch_update` | Sheets |
| `open("title")`, `openall`, `list_spreadsheet_files` | Drive, then Sheets |
| `create`, `copy`, `del_spreadsheet`, `share` | Drive |

### google-api-python-client

The official client takes an httplib2-style transport rather than a `requests`
session, so it uses a separate adapter:

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

`httplib2` is not a dependency of this library; the adapter wraps whichever
transport it is given.

### aiogoogle (async)

`aiogoogle` takes a session class rather than an instance, so the adapter is a
factory that subclasses one:

```python
from aiogoogle.client import Aiogoogle
from aiogoogle.sessions.aiohttp_session import AiohttpSession

from googleapis_without_429 import rate_limited_session

Session = rate_limited_session(AiohttpSession)


async def append_rows(creds, sheet_id, rows):
    async with Aiogoogle(session_factory=Session, user_creds=creds) as google:
        sheets = await google.discover("sheets", "v4")
        for row in rows:
            await google.as_user(
                sheets.spreadsheets.values.append(
                    spreadsheetId=sheet_id, range="A1", json={"values": [row]}
                )
            )
```

The quota belongs to the returned class rather than to its instances, because
`aiogoogle` constructs a session per operation; a per-instance limiter would
give each call a full quota. `aiogoogle` is not a dependency.

Retries in this adapter apply to single requests only. `aiogoogle` sends a
batch concurrently and raises one error for the whole set, which does not
identify the failing call.

`gspread-asyncio` is not supported: it runs synchronous gspread in a thread
pool and constructs its client without a session argument.

### Any other client

For calls made some other way — a hand-rolled client, a worker, an API with no
adapter here — use the limiter directly. It is both a context manager and a
decorator:

```python
from googleapis_without_429 import SHEETS, QuotaLimiter

limiter = QuotaLimiter([SHEETS])


@limiter.limit(SHEETS, "write")
def push_batch(rows): ...


with limiter.limit(SHEETS, "read"):
    ...
```

`acquire_async`, `acquire_for_async` and `limit_async` are the awaitable
equivalents, used the same way.

The underlying window is reachable directly:

```python
limiter.bucket(SHEETS, "write").acquire(cost=1)
limiter.bucket(SHEETS, "write").used  # units currently counted
```

### Sharing one limiter

A program using more than one client should pass a single limiter to all of
them, so they draw on one quota rather than one each:

```python
from googleapis_without_429 import QuotaLimiter, RateLimitedHttp, RateLimitedSession

limiter = QuotaLimiter()
session = RateLimitedSession(credentials, limiter=limiter)
http = RateLimitedHttp(authorised_http, limiter=limiter)
```

`acquire` and `acquire_async` share one lock and one set of buckets, so a
single limiter can be shared between threads and coroutines. A session also
exposes its own limiter, for pacing a call it does not make itself:

```python
session.limiter.bucket(SHEETS, "read").acquire()
```

## Retries

Pacing reduces `429` responses but does not eliminate them, so a retry is built
in and enabled by default.

The two sides count differently. This library slides a window over the
timestamps of its own calls; Google meters fixed windows whose boundaries are
not visible from the client. Sixty evenly spaced calls can land as 30 in the
tail of one Google minute and 30 in the head of the next, and the second group
exceeds the limit while the local counter still shows room. The limiter may
therefore allow fewer calls than Google would, never more.

```python
from googleapis_without_429 import RateLimitedSession, RetryPolicy

RateLimitedSession(credentials, retry=RetryPolicy(max_attempts=3))
```

| Field | Default | Meaning |
|---|---|---|
| `max_attempts` | `5` | total tries per request, including the first |
| `backoff_base` | `1.0` | ceiling for the first retry delay, in seconds |
| `backoff_cap` | `60.0` | the ceiling stops doubling here |
| `retry_after_cap` | `300.0` | longest `Retry-After` that will be honoured |
| `retry_server_errors` | `True` | retry 500, 502, 503 and 504 |
| `retry_unsafe_server_errors` | `False` | also retry `POST` on those |

Delays use equal jitter: half the ceiling is always waited, the rest is
randomised. The lower half is not randomised away because a `429` means the
window has not reopened, so a near-zero delay produces another `429`. A
`Retry-After` header, when present, takes precedence over the computed delay,
bounded by `retry_after_cap`.

### Drive answers 403, not 429

Sheets returns `429` when a quota is exceeded. Drive returns `403 Forbidden`
for the same condition and only sometimes `429`, so a retry policy keyed on
`429` alone misses Drive rate limiting entirely.

A `403` is also the ordinary response to a permission failure, so the status
code alone is not sufficient. The reason string in the response body is:

| Response | Retried | Reason |
|---|---|---|
| `429` | yes | unambiguous |
| `403` + `rateLimitExceeded` | yes | clears within the minute |
| `403` + `userRateLimitExceeded` | yes | clears within the minute |
| `403` + `dailyLimitExceeded` | **no** | resets at midnight Pacific |
| `403` + `sharingRateLimitExceeded` | **no** | not a short-term limit |
| `403`, any other reason | **no** | not a rate limit |

A body that is absent, not JSON, or shaped unexpectedly counts as not a rate
limit, so a malformed response cannot start a retry loop.

### Server errors and writes that must not repeat

Google's guidance recommends backoff for `500`, `502`, `503` and `504`. A 5xx
leaves it unknown whether the request took effect, so only methods that are
safe to repeat are retried:

| Method | Retried on 5xx | Reason |
|---|---|---|
| `GET`, `HEAD`, `OPTIONS` | yes | no side effect |
| `PUT`, `DELETE` | yes | idempotent |
| `POST` | **no** | repeating `values:append` adds the row twice |

Rate limits are unaffected by this rule: a `429` or a rate-limit `403` rejected
the request outright, so those are retried for any method.

To retry `POST` on 5xx as well, or to disable server-error retries:

```python
from googleapis_without_429 import RetryPolicy

RetryPolicy(retry_unsafe_server_errors=True)
RetryPolicy(retry_server_errors=False)
```

## Waiting, timeouts and skipping

By default a call waits as long as the quota requires. For request-serving
code, `acquire_timeout` bounds that wait:

```python
from googleapis_without_429 import QuotaTimeoutError, RateLimitedSession

session = RateLimitedSession(credentials, acquire_timeout=5.0)

try:
    session.get(url)
except QuotaTimeoutError as exc:
    print(f"gave up after {exc.waited:.1f}s waiting for {exc.cost} unit(s)")
```

`QuotaTimeoutError` subclasses the built-in `TimeoutError`, so existing
`except TimeoutError` handlers catch it. No quota is consumed when it raises.

For work that can be skipped instead of delayed, `try_acquire` takes quota only
if it is free:

```python
from googleapis_without_429 import SHEETS, QuotaLimiter

limiter = QuotaLimiter()


def refresh(spreadsheet_id):
    if limiter.try_acquire(SHEETS, "read"):
        return fetch_now(spreadsheet_id)
    return serve_cached(spreadsheet_id)
```

## Stats and logging

Every bucket records counters, with no configuration:

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

In this output the read bucket is the bottleneck: three waits totalling 58
seconds, against none on writes.

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

Callers are served in arrival order. Without an order, an expensive call can be
starved indefinitely: Drive charges 200 units for a download and 5 for a
metadata read, so cheap calls can keep the window full enough that the
expensive one never fits.

The queue costs throughput in one case — a cheap call that would fit now waits
behind an expensive one that does not. For Sheets nothing changes, because
every call there costs 1.

## Threads

`WeightedSlidingWindow` and `QuotaLimiter` are thread-safe, and the test suite
exercises them on the free-threaded build (3.14t) in CI.

`RateLimitedSession` inherits from `requests.Session`, which makes no
thread-safety promise. Use one session per thread with a shared limiter;
otherwise each session keeps its own quota and the effective limit is
multiplied by the number of threads:

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

Eight threads then consume the same 60 reads a minute that one thread would.

## Adjusting the limits

The shipped figures are Google's documented defaults. Real quotas depend on the
project and on when it was created — Drive's changed on 1 May 2026, and
projects already using the API kept the previous ones. Limits are overridable
per profile:

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

The defaults are the **per-user** quotas, which is what a single script reaches.
The per-project ceiling applies only if the job is the sole user of that
project. An unknown bucket name raises `ValueError` rather than being ignored.

Project quotas are listed in the Cloud Console under
**APIs & Services → Quotas**, and can differ from the documentation.

### When the metering model differs

`with_limits` changes limit values only. Drive counted requests before
1 May 2026 and counts weighted units after; the two schemes are not
convertible, so a project on the older one needs its own profile with its own
`resolve` and `window`:

```python
from googleapis_without_429 import ApiProfile, RateLimitedSession

DRIVE_LEGACY = ApiProfile(
    name="drive",
    host="www.googleapis.com",
    path_prefixes=("/drive/", "/upload/drive/"),
    limits={"queries": 12_000},  # the project's real figure, from the Console
    resolve=lambda method, path, query: ("queries", 1),  # requests, not units
    window=100.0,  # some legacy quotas are metered per 100 seconds
)

session = RateLimitedSession(credentials, [DRIVE_LEGACY])
```

The window belongs to the profile, so one limiter can hold a per-minute quota
and a per-100-seconds quota at the same time.

## Adding an API

A profile is data: a host, a map of buckets to limits, and a function returning
the bucket and cost for a call.

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

Two constraints on `resolve`:

- **The HTTP method does not always identify the operation.** Sheets sends
  several reads as POST (`:getByDataFilter`, `:batchGetByDataFilter`,
  `developerMetadata:search`) because they carry a request body. Charging those
  to the write bucket consumes one of 60 writes a minute. An API's published
  [discovery document](https://developers.google.com/discovery/v1/reference)
  lists every method with its real verb and path.
- **A host may serve several APIs.** Drive shares `www.googleapis.com` with
  others, so its profile sets `path_prefixes=("/drive/", "/upload/drive/")`.
  Leave that empty only when the host belongs to a single API.

Profiles for other Google APIs are welcome as pull requests; adding one
requires no changes to the core.

## Development

```bash
uv sync                      # install the project and its dev dependencies
make check                   # everything CI runs: lint, format, types, tests
uv run pre-commit install    # optional: fast checks on every commit
```

Individual steps: `make lint`, `make check-format`, `make typecheck`,
`make test`. CI calls the same targets. The test suite needs no credentials and
makes no network calls.

## Roadmap

- **Calendar and Docs profiles.** Blocked on verifying each API's published
  quotas.
- **A Drive profile for projects on the pre-May-2026 quota**
  ([#1](https://github.com/pavlosambur/googleapis-without-429/issues/1)). That
  scheme counted requests rather than weighted units. No default is shipped
  because the figure cannot be verified without a project still on the old
  quota; the machinery is documented under
  [When the metering model differs](https://github.com/pavlosambur/googleapis-without-429#when-the-metering-model-differs).
- **Batch retries for the async adapter.** `aiogoogle` raises one error for a
  concurrent batch, which does not identify the failing request.

## License

MIT
