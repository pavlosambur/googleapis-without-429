# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/pavlosambur/googleapis-without-429/commits/main/
