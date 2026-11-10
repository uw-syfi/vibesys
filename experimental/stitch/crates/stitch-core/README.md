# stitch-core

Pure evidence model, graph construction, correlation, and trace and CPU sample
queries. The core has no profiler- or language-specific dependencies. Spans may
carry importer-derived tracing detail (kind, operation, failure, datastore,
links), and a graph may carry the run's measurement windows.
