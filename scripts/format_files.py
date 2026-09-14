"""Run text formatters on Git-visible project files with literal path arguments."""

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUFFIXES = {
    "md": {".md"},
    "toml": {".toml"},
    "yaml": {".yml", ".yaml"},
    "json": {".json"},
    "html": {".html"},
    "css": {".css"},
}


def project_files(kind: str) -> list[str]:
    """Find existing files without traversing ignored caches or template sources."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = {Path(name.decode()) for name in result.stdout.split(b"\0") if name}
    return [
        f"./{path.as_posix()}"
        for path in sorted(paths)
        if path.parts[0] != "template"
        and path.suffix in SUFFIXES[kind]
        and path.name != "uv.lock"
        and (ROOT / path).is_file()
        and not (ROOT / path).is_symlink()
    ]


def format_css(paths: list[str], *, check: bool) -> int:
    """Compare CSS beautifier output without rewriting files during a check."""
    changed = False
    for name in paths:
        result = subprocess.run(
            ["css-beautify", "--indent-size", "2", "--end-with-newline", name],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        path = ROOT / name
        if path.read_text() != result.stdout:
            changed = True
            print(f"{'Would format' if check else 'Formatted'} {name}")
            if not check:
                path.write_text(result.stdout)
    return int(check and changed)


def format_files(kind: str, *, check: bool) -> int:
    """Delegate formatting and validation to the installed tools."""
    paths = project_files(kind)
    if not paths:
        return 0
    if kind == "css":
        return format_css(paths, check=check)
    commands = {
        "md": ["mdformat"],
        "toml": ["taplo", "fmt"],
        "yaml": ["yamlfix"],
        "json": ["pretty-format-json", "--indent", "2", "--no-sort-keys", "--no-ensure-ascii"],
        "html": [
            "djlint",
            "--profile",
            "html",
            "--indent",
            "2",
            "--indent-css",
            "2",
            "--indent-js",
            "2",
            "--max-line-length",
            "120",
            "--format-css",
            "--format-js",
        ],
    }
    command = commands[kind]
    options = ["--check"] if check else []
    if kind == "json":
        options = [] if check else ["--autofix"]
    elif kind == "html":
        options = ["--check"] if check else ["--reformat"]
    result = subprocess.run([*command, *options, *paths], cwd=ROOT, check=False)
    # These CLIs also return 1 after successful rewrites; verify the result.
    if kind in {"json", "html"} and not check and result.returncode:
        verify_options = ["--check"] if kind == "html" else []
        return subprocess.run([*command, *verify_options, *paths], cwd=ROOT, check=False).returncode
    return result.returncode


def main() -> int:
    """Parse the Make recipe's formatter kind and optional check mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=SUFFIXES)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    return format_files(arguments.kind, check=arguments.check)


if __name__ == "__main__":
    raise SystemExit(main())
