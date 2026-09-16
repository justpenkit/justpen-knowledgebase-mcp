# APSW typing compatibility overlay

This directory contains the complete type stub from APSW 3.53.4.0, sourced
from <https://github.com/rogerbinns/apsw/blob/3.53.4.0/apsw/__init__.pyi>.
The original stub's SHA-256 is
`bb9c1f61f3a2d5f1e7128771b0807792cce66a48d1146251a1c6c6f4f5707a47`.

The local copy is altered only to import `Buffer` from `typing_extensions` on
Python below 3.12, where `collections.abc.Buffer` is unavailable, and to
normalize the upstream trailing blank lines to one final newline. The
integration test checks the installed APSW version and source hash and then
requires exact byte parity after applying only those two corrections.

This is a static-analysis overlay; the installed APSW package remains the
runtime implementation. Remove the overlay, its test, license, and direct
development dependency when the locked APSW release has a Python 3.11
compatible stub.
