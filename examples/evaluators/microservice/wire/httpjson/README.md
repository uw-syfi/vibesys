# Canonical JSON-over-HTTP requests

This package owns the observable request encoding shared by benchmark and
accuracy modes. It applies one header policy and canonicalizes JSON so a struct
and an equivalent map have identical wire representations. Application paths,
authentication semantics, and response expectations remain in application
adapters.
