---
title: One real-world entity has exactly one accepted identity spelling
applies_when:
  - adding or changing an identity-bearing property grammar in the catalog
  - accepting a host, URL, purl, advisory id or other identifier form
  - reviewing a catalog change that widens what an identity property accepts
tags: [catalog, identity]
---

An identity grammar accepts one spelling per entity, so one entity is one node. When a second spelling names the same entity, the grammar refuses it, and the error points at the canonical form. Spellings refused this way include provider alias hostnames, ECR FIPS and dual-stack registry hosts, Docker Hub's alternate hosts, and an explicit default port such as `:443`. When the alias itself is a fact worth recording, it gets its own node, linked by a relation. An alias hostname, for example, is a `subdomain` reached through `hosted_on`.

Before widening a grammar, list the spellings the change admits and confirm none of them names an entity that another accepted spelling already names.
