# Commands

This directory contains bundled executable composition roots. A command selects
concrete application adapters and protocol drivers, then delegates flag parsing
and execution to `servicebenchcli`.

Reusable scheduling, transport, configuration, and CLI behavior must remain in
their owning packages rather than accumulating in a command. Task-specific
correctness and its composition root belong with the task or example. This
keeps bundled commands thin and prevents the generic command from importing
every task oracle.

`servicebench/` supplies the legacy bundled registrations to
`servicebenchcli.Run`. New task commands should use the same API with their own
`composition.Registration` values.
