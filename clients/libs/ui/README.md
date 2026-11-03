# Design system

This package owns domain-free React components, typed design tokens, themes,
fonts and the styles generated from them. Apps compose its public components
with their own data and callbacks. Shared components do not import the run model
or perform I/O.

The public component and prop exports are in [the package entrypoint](src/index.ts).
Keep that surface authoritative instead of maintaining a second prop inventory.
Components take token-based variants and callbacks; apps do not supply arbitrary
styles. Time-dependent values are formatted by the caller and passed as data.

The token tables own each scale and semantic color role. Generated styles derive
from those tables. Theme and contrast tests exercise the public token API; the
component gallery covers visual states independently of the desktop.

See [Clients architecture](../../../docs/contributing/clients-architecture.md)
for runtime boundaries, design-system enforcement and verification roles.
