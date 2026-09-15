# API reference

These sections are generated from the server's Python docstrings. Update the
docstrings alongside public API changes, then run `make docs-build` to check the
reference and internal links.

## Configuration

::: justpen_knowledgebase_mcp.config

## Errors

::: justpen_knowledgebase_mcp.errors

## Response helpers

::: justpen_knowledgebase_mcp.responses

## Application lifecycle

::: justpen_knowledgebase_mcp.app

::: justpen_knowledgebase_mcp.service

The registry is currently empty while the real knowledgebase tools are implemented.
Successful tool results use `{"status":"ok","data":...}`; errors use
`{"status":"error","error":"CODE: message"}` with bounded operational details.
