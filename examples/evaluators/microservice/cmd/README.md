# Commands

This directory contains executable composition roots for the evaluator. A
command may select concrete application adapters and protocol drivers, parse
user-facing flags, and connect those pieces to the shared engine.

Reusable scheduling, transport, configuration, and application behavior must
remain in their owning packages rather than accumulating in a command. This
keeps the command thin and makes the core testable with fake extensions.

`microbench/` is currently the only command.
