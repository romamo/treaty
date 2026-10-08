# Security policy

## Supported versions

Security fixes land in the latest release. Until 1.0, that is the latest release
candidate; upgrade to it before reporting.

| Version | Supported |
| --- | --- |
| latest 1.0 release candidate | Yes |
| older releases | No |

## Reporting a vulnerability

Report it privately through
[GitHub's private vulnerability reporting](https://github.com/romamo/treaty/security/advisories/new),
never in a public issue, pull request, or discussion.

Include the treaty version, a minimal app that shows the problem, the command you ran, and
what it exposed or allowed. You can expect a first reply within 7 days. Once a fix is
released, the advisory is published with credit to you, unless you prefer otherwise.

## Scope

In scope: the framework's own guarantees, such as secrets leaking into output, logs, or
the audit log; a destructive command running without confirmation; path validation that
lets a value escape its directory; and the MCP adapter exposing a command registered
`mcp=False`.

Out of scope: vulnerabilities in an app built with treaty that come from its own handler
code, unless treaty's documented guarantees should have prevented them.
