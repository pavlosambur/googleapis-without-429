# googleapis-without-429

Drop-in transport for Google API clients that stays inside the quota instead of
recovering from `429 Too many requests`.

> **Status: work in progress.** Nothing is released yet. The first release will
> cover the Google Sheets API through a `requests`-based session (which is what
> [gspread](https://github.com/burnash/gspread) uses).

## License

MIT
