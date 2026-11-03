### Added

- `ctx.http`: `handle=` on `request`, `get`, and `post` returns a 401, 403, or 429 to the
  handler with its headers and body, such as RFC 9457 problem details, instead of ending
  the run with the declared `AUTH_REQUIRED`, `PERMISSION_DENIED`, or `RATE_LIMITED`; any
  other status in it raises `ValueError` before the request is sent (#449)
