"""Indexed ownership keys; unknown metadata conservatively blocks orphan disposal."""

BLOB_LOCATOR_KEY = """CASE
 WHEN NOT json_valid(progress) THEN '!invalid'
 WHEN json_type(progress)!='object' THEN '!invalid'
 WHEN json_type(json_remove(progress,'$.verified_sha256'),'$.verified_sha256') IS NOT NULL THEN '!invalid'
 WHEN json_type(progress,'$.verified_sha256') IS NULL THEN NULL
 WHEN json_type(progress,'$.verified_sha256')='text'
  AND length(json_extract(progress,'$.verified_sha256'))=64
  AND json_extract(progress,'$.verified_sha256') NOT GLOB '*[^0-9a-f]*'
 THEN json_extract(progress,'$.verified_sha256')
 ELSE '!invalid' END"""

# Only the shared static expression is assembled here; runtime values are bindings.
OTHER_BLOB_OWNER_SQL = "\n".join(
    (
        "SELECT 1 FROM jobs WHERE purge_pending=0 AND (",
        BLOB_LOCATOR_KEY,
        ") IN (?, '!invalid') AND uuid<>? LIMIT 1",
    )
)
