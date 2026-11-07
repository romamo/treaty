### Changed

- `McpServe(tools=...)` serves a provided tool named like a command the run keeps off the
  server (left out by `McpServe(commands=)` or registered `mcp=False`), such as an API's
  own `whoami`: that name answers for the provided tool, and the command still never runs
  over MCP by any name. A provided tool named like a served command's tool, a
  `requires_person` command, or another provided tool is still refused with
  `MCP_TOOL_NAME_TAKEN`, whose message now says which (#453)
