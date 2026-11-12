# stitch-session

Manifest validation and import orchestration. The session receives importer
and source mapper registries from its caller; perf and OTel capture belong to
their plugins. Manifest windows are validated here and copied into the graph.
