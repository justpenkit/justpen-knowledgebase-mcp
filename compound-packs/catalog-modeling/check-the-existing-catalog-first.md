---
title: Check that the existing catalog cannot already model a fact before proposing a new type
applies_when:
  - proposing a new node type, relation or property for the catalog
  - brainstorming or planning catalog coverage for a pentest, ASM or EASM finding
  - reviewing a plan or change that adds a type, relation or property to catalog.py
tags: [catalog, modeling]
---

Before proposing a new node type, relation or property, show how the fact would be written with the types the catalog already has, and propose the addition only when that attempt fails. Name the existing types the attempt tried and the specific gap that blocked it. A new type that renames a combination the graph already expresses is refused.

Examples the catalog already covers: a subdomain takeover is a finding on the subdomain, not a takeover node; a web origin is a domain or subdomain resolving to an IP, with the port attached to the IP. Virtual hosting is many-to-many between names and IPs, and the existing relations already express it.
