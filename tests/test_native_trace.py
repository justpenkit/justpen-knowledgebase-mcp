"""Isolated parser recognizes native writes without confusing devices/pipes with files."""

from pathlib import Path

import pytest

from .integration.native_trace import external_mutations


@pytest.mark.parametrize(
    "line",
    [
        '1 openat(AT_FDCWD, "/workspace/db", O_RDWR|O_CREAT, 0600) = 3</workspace/db>',
        '1 write(3</workspace/db-wal>, "bytes", 5) = 5',
        '1 write(2<pipe:[123]>, "diagnostic", 10) = 10',
        '1 openat(AT_FDCWD, "/outside/module.py", O_RDONLY) = 3</outside/module.py>',
        '1 openat(AT_FDCWD, "/outside/denied", O_RDWR|O_CREAT) = -1 EACCES (Permission denied)',
        '1 write(3</dev/null>, "data", 4) = 4',
        '1 write(4<anon_inode:[eventfd]>, "notification", 8) = 8',
    ],
)
def test_non_external_mutations(line):
    assert external_mutations(line, Path("/workspace")) == []


@pytest.mark.parametrize(
    "line",
    [
        '1 openat(AT_FDCWD, "/outside/cache", O_WRONLY|O_CREAT, 0600) = 3</outside/cache>',
        '1 rename("/workspace/stage", "/outside/blob") = 0',
        '1 unlink("/outside/blob") = 0',
        '1 mkdir("/outside/cache", 0700) = 0',
        "1 ftruncate(3</outside/db>, 0) = 0",
        '1 pwrite64(3</outside/db>, "x", 1, 0) = 1',
        '1 write(8, "unknown descriptor", 18) = 18',
    ],
)
def test_external_mutations_or_unresolved_write_are_reported(line):
    assert external_mutations(line, Path("/workspace")) == [line.removeprefix("1 ")]


def test_at_cwd_is_not_a_mutation_and_buffer_strings_are_not_paths():
    assert external_mutations('1 unlinkat(AT_FDCWD</work>, "/workspace/journal", 0) = 0', Path("/workspace")) == []
    assert (
        external_mutations('1 pwrite64(3</workspace/db>, "/outside-looking bytes", 22, 0) = 22', Path("/workspace"))
        == []
    )


def test_relative_escape_and_resumed_calls_are_not_lost():
    outside = '1 renameat(3</workspace>, "stage", 3</workspace>, "../escaped") = 0'
    assert external_mutations(outside, Path("/workspace"))
    trace = '1 openat(AT_FDCWD</work>, "/outside/new", O_WRONLY|O_CREAT <unfinished ...>\n2 write(2<pipe:[1]>, "log", 3) = 3\n1 <... openat resumed>, 0600) = 5</outside/new>'
    assert len(external_mutations(trace, Path("/workspace"))) == 1


@pytest.mark.parametrize(
    "operation",
    [
        'unlink("/workspace/../outside/victim")',
        'rename("/workspace/stage", "/workspace/../outside/blob")',
        'mkdir("/workspace/../outside/directory", 0700)',
        'unlink("/workspace/possible-symlink/../victim")',
    ],
)
def test_absolute_parent_traversal_is_never_a_containment_proof(operation):
    line = operation + " = 0"
    assert external_mutations(line, Path("/workspace")) == [line]
