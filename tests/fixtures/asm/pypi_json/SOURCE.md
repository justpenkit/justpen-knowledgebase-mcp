# pypi_json

- **Command:** `curl -s https://pypi.org/pypi/Flask-SQLAlchemy/json`
- **Version:** PyPI JSON API (Warehouse), unauthenticated
- **Schema:** [Project](https://docs.pypi.org/api/json/#get-a-project): `info` (the latest release's core metadata
    plus PyPI's URLs), `last_serial`, `ownership` (the owning organization and its user roles), `releases` (every
    version's files), `urls` (the latest release's files) and `vulnerabilities` (OSV records affecting the latest
    release).

Recorded from the live API on 2026-10-01 and pretty-printed; no value is changed. The project is owned by the
`pallets-eco` organization, and its only contact is the organization mailbox `Pallets <contact@palletsprojects.com>`;
the author fields are empty, `ownership.roles` is empty and the description names no person, so the response holds
no personal data to replace.

## Notes

- The package is the versionless purl of `info.name`, normalized by the PyPA rule (`pypi_name_to_purl`):
    `Flask-SQLAlchemy` is `pkg:pypi/flask-sqlalchemy`. It is written as a `candidate`, since a registry listing does
    not show the target publishes it.
- `info.author_email` and `info.maintainer_email` are the package's listed contacts and each becomes an
    `email_address` with a `has_contact` edge of role `maintainer`; an empty field writes nothing.
- The `project_urls` label of the source repository is chosen by the project (`Source Code` here, `Source` or
    `Repository` elsewhere). Its GitHub URL becomes a `repository` and a `published_from` edge; every other project
    URL stays in the evidence.
- `info.version`, `releases` and `urls` describe releases, which stay in the evidence: one package node stands for
    every release.
- `vulnerabilities` is empty for this release; a project with known vulnerabilities lists OSV records there, which
    this fixture does not exercise.
