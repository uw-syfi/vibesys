# Composition helpers

This package provides typed functions for registering protocol drivers,
benchmark applications, and accuracy applications in a servicebench registry.
It has no built-in registrations and imports no concrete implementation.

Executable commands are composition roots. The bundled command supplies its
legacy registrations; a task-owned command supplies the driver and application
implementations needed by that task. Duplicate, invalid, or nil registrations
fail while the registry is built.

Command-line behavior does not belong here. Use `servicebenchcli.Run` after
declaring the command's `Registration` values.
