---
title: Refuse a non-canonical spelling unless a declared canonicalize rule rewrites it
applies_when:
  - handling a non-canonical spelling of an identity value in a catalog validator
  - adding or changing a canonicalize rule in catalog.py
  - reviewing a validator that lowercases, trims or rewrites input
tags: [catalog, identity, canonicalization]
---

A validator never silently rewrites a value into its canonical form. A non-canonical spelling is refused with an error that names the canonical form. The only exception is a declared, versioned rule in the type's `canonicalize` list, such as `endpoint_url_drop_query.1` or `caa_parameter_name_fold.1`. That rule runs before validation, identity and storage, and it must not widen what validation accepts. Changing a rule's behavior bumps its version suffix, which changes the catalog fingerprint.
