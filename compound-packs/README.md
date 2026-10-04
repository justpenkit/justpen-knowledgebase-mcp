# Compound Packs

Project rules that Compound Engineering's planning and review skills read and
cite. `.compound-engineering/config.yaml` declares this folder with
`packs: - source: compound-packs`.

Each subfolder is one pack. A pack holds one rule per top-level Markdown file,
each with `title` and `applies_when` frontmatter, plus an optional `README.md`
describing the pack. This README is not a rule.

Until the first pack exists, CE reports that `compound-packs` publishes no packs.
The warning is expected and does not stop any skill.
