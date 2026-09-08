# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `WeightedSlidingWindow`: thread-safe core limiter that counts cost rather
  than calls, so one implementation covers both request-counted APIs (Sheets)
  and quota-unit APIs (Gmail).

[Unreleased]: https://github.com/pavlosambur/googleapis-without-429/commits/main/
