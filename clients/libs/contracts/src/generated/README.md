# Generated files

Files in this directory are generated from authoritative Python contracts and
must not be modified directly.

Regenerate them from the repository root with:

```bash
pnpm --dir clients/libs/contracts generate:protocol
uv run python scripts/generate_architecture_graph_contract.py \
  --schema clients/libs/contracts/src/generated/architecture-graph.schema.json \
  --module clients/libs/contracts/src/generated/architecture-graph.schema.ts
pnpm --dir clients exec json2ts \
  -i libs/contracts/src/generated/architecture-graph.schema.json \
  -o libs/contracts/src/generated/architecture-graph.generated.ts \
  --bannerComment '/* Generated from vibesys.api.architecture. Do not edit. */'
```
