"""Conservative strace audit using completed calls and -yy resolved descriptors."""

import ast
import posixpath
import re
from pathlib import Path

OPERATIONS = r"openat|open|creat|renameat2?|rename|unlinkat|unlink|mkdirat|mkdir|truncate|ftruncate|pwrite64|write|chmod|fchmod|symlinkat|symlink|linkat|link"
QUOTED = re.compile(r'"(?:\\.|[^"\\])*"')


def completed_calls(trace):
    pending = {}
    for line in trace.splitlines():
        prefix = re.match(r"(\d+)\s+(.*)", line)
        pid, body = (prefix[1], prefix[2]) if prefix else ("single", line)
        if "<unfinished ...>" in body:
            pending[pid] = body.split("<unfinished ...>")[0]
            continue
        resumed = re.match(r"<\.\.\. \w+ resumed>(.*)", body)
        if resumed:
            body = pending.pop(pid, "") + resumed[1]
        yield body
    # An incomplete successful/failed disposition cannot be assumed safe.
    yield from ("UNRESOLVED " + body for body in pending.values())


def descriptor_path(value):
    match = re.search(r"<(/[^>]+)>", value)
    return match[1].removesuffix(" (deleted)").split("<", 1)[0] if match else None


def mutation_paths(name, body):
    if name in {"write", "pwrite64", "ftruncate", "fchmod"}:
        descriptor = body.split("(", 1)[1].split(",", 1)[0]
        path = descriptor_path(descriptor)
        if path:
            return [path]
        return (
            []
            if any(
                kind in descriptor
                for kind in ["pipe:[", "socket:[", "<UNIX-", "<TCP:", "<UDP:", "<anon_inode:[eventfd]>"]
            )
            else [None]
        )
    if name in {"open", "openat", "creat"}:
        if name != "creat" and not re.search(r"O_WRONLY|O_RDWR|O_CREAT|O_TRUNC", body):
            return []
        return [descriptor_path(body.rsplit(" = ", 1)[-1])]
    paths = []
    for match in list(QUOTED.finditer(body))[: 2 if name.startswith(("rename", "link", "symlink")) else 1]:
        value = ast.literal_eval(match[0])
        if not value.startswith("/"):
            prefix = body[: match.start()]
            bases = re.findall(r"<(/[^>]+)>", prefix)
            value = posixpath.join(bases[-1], value) if bases else None
        # Parent traversal can cross a symlink before returning lexically inside.
        # No filesystem lookup after the trace can establish that historical resolution.
        paths.append(None if value is None or ".." in value.split("/") else posixpath.normpath(value))
    return paths or [None]


def external_mutations(trace: str, root: Path) -> list[str]:
    """Return outside writes/unknown resolutions; pipe/socket/device IO is separate."""
    violations = []
    for body in completed_calls(trace):
        if re.search(r"= -1\b", body):
            continue
        operation = re.search(r"\b(" + OPERATIONS + r")\(", body)
        if operation is None:
            continue
        paths = mutation_paths(operation[1], body)
        if body.startswith("UNRESOLVED") or any(
            path is None
            or (Path(path) not in {Path("/dev/null"), Path("/dev/tty")} and not Path(path).is_relative_to(root))
            for path in paths
        ):
            violations.append(body)
    return violations
