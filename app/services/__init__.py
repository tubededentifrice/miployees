"""Cross-context service facades.

Each subpackage under ``app.services.<context>`` re-exports the domain
service's public surface as the **only** allowed import path from
other bounded contexts. Domain services stay free to restructure
their internals; consumers reach across the context boundary via
:mod:`app.services.<context>` only.

See ``docs/specs/01-architecture.md`` §"Contexts & boundaries".
"""
