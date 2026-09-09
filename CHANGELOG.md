# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.3.0] - 2026-09-09

### Added

- Waiting can be bounded. `acquire(timeout=...)` and
  `RateLimitedSession(acquire_timeout=...)` raise `QuotaTimeoutError` rather
  than blocking indefinitely, which is what anything serving a request needs:
  a handler stalled for fifty seconds is indistinguishable from a hung process.
  The error subclasses the built-in `TimeoutError`, and no quota is consumed
  when it raises.
- `try_acquire()` consumes quota only if it is free right now, for work that
  can be skipped or queued instead of waited on.
- Every bucket keeps counters — granted, waits, total wait time, timeouts —
  reachable through `limiter.stats()`. A limiter doing its job looks exactly
  like a hung program, so being able to see the waiting is not optional.
- Waiting is logged at `DEBUG` and retries at `INFO`, under the
  `googleapis_without_429` logger.
- Server errors (500, 502, 503, 504) are retried, but only for methods that are
  safe to repeat. A 5xx leaves it unknown whether the request took effect, so
  repeating `values:append` could add the row twice — `GET`, `HEAD`, `OPTIONS`,
  `PUT` and `DELETE` are retried and `POST` is not. Rate limits are unaffected:
  a 429 rejected the request outright, so it is retried whatever the method.
  `RetryPolicy(retry_unsafe_server_errors=True)` opts POST in;
  `RetryPolicy(retry_server_errors=False)` opts everything out.

- Callers are served in arrival order. Costs differ by a factor of forty on
  Drive, and without a queue cheap calls keep the window just full enough that
  an expensive one never fits. The queue trades a little throughput for the
  guarantee that every call eventually runs; for Sheets, where every call costs
  one, it changes nothing.

### Changed

- Retry settings moved from four constructor arguments to a `RetryPolicy`
  object: `RateLimitedSession(credentials, retry=RetryPolicy(max_attempts=3))`.
  The old `max_attempts`, `backoff_base`, `backoff_cap` and `retry_after_cap`
  arguments are gone. Adding server-error handling would have made six loose
  arguments, and the policy is also the natural place for the next question of
  this kind.

## [0.2.0] - 2026-09-09

### Fixed

- Rate limits from the Drive API are now retried. Drive answers `403 Forbidden`
  with a reason of `rateLimitExceeded` or `userRateLimitExceeded` where Sheets
  answers `429`, so a retry watching only for 429 did nothing on exactly the
  calls it was meant to protect. A 403 is retried only when its body names a
  short-term limit: `dailyLimitExceeded` (which resets at midnight Pacific),
  `sharingRateLimitExceeded`, and every permission error are returned
  unchanged, since retrying a refusal turns a clear failure into a slow one.

### Added

- `window` is now a property of a profile rather than of the limiter, so one
  limiter can hold a per-minute quota and a per-100-seconds quota at once. This
  is what a project still on Drive's pre-May-2026 quota needs: that scheme
  counted requests rather than weighted units, and no single number converts
  between the two.
- `googleapis_without_429.errors` exposes `is_rate_limited` and
  `response_reasons` for code that needs the same classification elsewhere.
- The test suite runs on the free-threaded build (3.14t) in CI, with tests that
  check the limiter's invariants under real parallelism.
- `RateLimitedSession(..., limiter=...)` shares one set of quota buckets across
  several sessions. Threaded code wants a session per thread, and without a
  shared limiter each would keep its own quota, multiplying the effective limit
  by the number of threads.

## [0.1.0] - 2026-09-09

### Added

- `WeightedSlidingWindow`: thread-safe core limiter that counts cost rather
  than calls, so one implementation covers both request-counted APIs (Sheets)
  and quota-unit APIs (Gmail).
- `ApiProfile` and the `SHEETS` profile: per-API data describing the host, its
  separate read and write quotas, and how a call maps to one of them. Limits
  are overridable with `SHEETS.with_limits(read=300, write=300)`, since real
  quotas depend on the project and Google revises them.
- `RateLimitedSession`: an `AuthorizedSession` that paces itself against a
  profile's quotas and retries a 429 with equal-jitter backoff, honouring
  `Retry-After` when the server sends one. Requests to hosts without a profile
  pass through untouched, so token refreshes do not consume the API's quota.
  Drop it into any client that accepts a session, such as gspread.
- `DRIVE` profile, covering the half of gspread that is not Sheets: creating,
  deleting, sharing and finding a spreadsheet by title all go to the Drive API.
  Drive meters weighted quota units in one shared bucket rather than counting
  requests in two, so a listing costs twenty times a single item read.
- Profiles can claim path prefixes, since `www.googleapis.com` serves several
  APIs and a host alone no longer identifies which quota applies.
- `QuotaLimiter`: the quota buckets on their own, for code that does not go
  through a `requests` session. `limiter.limit(SHEETS, "write")` works both as
  a context manager and as a decorator, and `limiter.bucket(...)` exposes the
  underlying window for anything this library does not model. A session's own
  buckets are reachable through `session.limiter`.

[Unreleased]: https://github.com/pavlosambur/googleapis-without-429/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/pavlosambur/googleapis-without-429/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/pavlosambur/googleapis-without-429/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/pavlosambur/googleapis-without-429/releases/tag/v0.1.0
