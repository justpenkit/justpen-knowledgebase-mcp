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

## Graph mutation contract

The service stores one node per validated catalog identity across the workspace.
Properties may contain additional JSON fields; those fields never change the
server-generated key. An ID patch retains omitted metadata, while explicit
`null` clears an optional label or source.

Object patches recursively merge. An empty object does not clear existing children:

```json
{"properties":{"scanner":{}}}
```

Given `{"scanner":{"status":"seen","note":"old"}}`, clear the object by
removing its child properties:

```json
{"remove_properties":["/scanner/status","/scanner/note"]}
```

The resulting property is `{"scanner":{}}`. Removing `/scanner/note` while
writing `/scanner/status` is valid. Removing `/scanner/status` while writing that
same path, or replacing `/scanner`, is invalid. Arrays are replaced as complete
values; removing an individual array element is unsupported. Required identity
fields cannot be removed or changed by a normal patch.

`observed_at` follows the last writer's payload, even when it describes an older
observation. It is not a maximum observation timestamp. Record reads preserve
whole properties and return `remaining_ids` when the response budget is reached.
Association cursors paginate live data without holding a snapshot or granting
additional access.
