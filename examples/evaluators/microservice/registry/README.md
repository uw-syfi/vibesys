# Extension registry

This package maps workload names to concrete protocol drivers and application
factories. It is the small indirection layer that lets the engine remain
independent of built-in implementations.

Registration rejects empty and duplicate names. Lookup rejects unsupported
protocols or applications and lists the registered choices to make workload
configuration errors actionable. Factories must return a non-nil adapter whose
reported identity exactly matches the selected workload key, preventing a
misregistered checker from validating the wrong application.

The command owns registry population. The registry does not discover plugins,
silently fall back, or contain benchmark behavior.
