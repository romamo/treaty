### Changed

- Contributors: `tests/test_public_api.py` also compares the public API snapshot with
  `tests/data/public_api.released.json`, the snapshot as of the last stable release; a
  removed or changed entry fails CI unless the changelog since that release has a
  `### Breaking` entry, so the release bumps the major version. An added name, a new
  optional or variadic parameter, and a new dataclass field with a default appended
  after the others still pass. `docs/api.md` says how the check reads a change and how
  to refresh the baseline after a stable release (#464)
