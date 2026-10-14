# Campaign architecture graph

The campaign architecture diagram is a historical projection, not a second
source of run state. Its topology and profiler attribution use the versioned
contract in `vibesys.api.architecture`; TypeScript types and the client schema
descriptor are generated from that contract. The campaign parser validates the
descriptor and its cross-references before the record reaches presentation.

## Ownership and data flow

Data flows in one direction:

1. A producer emits an `ArchitectureGraphDocument` through the Python public API.
2. The committed JSON Schema and TypeScript artifacts are generated from that
   Pydantic model.
3. `@vibesys/contracts` rejects structurally or semantically invalid documents.
4. The campaign feature folds its existing measurement cursor over validated
   observations and derives a deterministic layout.
5. The Web renderer only presents the resulting projection.

The Python public API is the named `vibesys.api.architecture` facade. Client
code imports the generated model and parser through `@vibesys/contracts`, and
apps reach cursor, selection, layout, and rendering only through the campaign
feature's public `./state` and `./renderer` entries.

The graph contract deliberately lives outside the run-event protocol. A future
transport can carry this document without making the server API or browser own
a parallel wire model. Live records use `null` until that transport exists.

## Version 1 semantics

`schema_version` is required and currently accepts only `1`. Component and edge
IDs are stable semantic identities; coordinates are never identities. Every
component has a path, module, or service anchor. Edges and observations may only
reference declared components, IDs are unique, self-edges are rejected, changed
paths are normalized relative paths, and observation sequences strictly increase.

Topology is shared across the document. Each observation is aligned to a
campaign measurement ID and records only component change annotations and the
cost or latency attribution available at that capture. Each change annotation
pairs one component ID with its recorded paths, so the highlighted component
set and displayed changed-file list cannot drift independently. Missing metrics
are represented by `null`, never by zero or an inferred value. When a campaign
cursor falls between captures, the projection uses the latest earlier capture;
before the first capture it presents an explicit unavailable state.

The layout is a pure, deterministic dependency-layer projection. It sorts all
semantic identities before placement and uses a stable fallback layer for cycles.
Responsive behavior belongs to CSS: narrow viewports retain the same layout in a
named, keyboard-focusable scroll region rather than silently dropping nodes.
Textual relationships accompany the visual edges for assistive technology.

Component selection is owned by the campaign XState machine, just like agent
and workstream selection. Each graph node is a keyboard-operable button whose
stable `aria-controls` target opens the campaign's existing Sheet detail
surface; selection survives measurement scrubbing, while the sheet's metrics
and changed paths continue to follow the shared cursor. The public selection
action also gives a future route adapter one stable ID to restore without
putting URL or browser state into the feature.

The default Web replay commits an explicitly agent-authored topology assembled
only from names already present in that replay's objective and workstreams. It
claims no measurements, so the visible demo also exercises the honest
unavailable state. The queue and hotel-search fixtures remain the realistic
measurement fixtures used for scrubbing, attribution, and rendering tests.

## Generation approach evaluation

Four producer approaches were considered:

- Static analysis is deterministic and explains topology well, but cannot
  establish runtime cost or latency by itself.
- Profiler-derived generation provides measured attribution, but incomplete
  traces can omit inactive components and cannot reliably recover ownership.
- Agent-authored generation can describe intent, but is not authoritative enough
  for identities, references, or measurements.
- A hybrid preserves static topology and stable anchors, joins recorded profiler
  facts where they exist, and uses the campaign's recorded changed paths for
  highlights. Agent output may suggest labels or groupings only after validation.

The recommended producer is therefore hybrid. The contract records its approach
and provenance so clients never need to guess which facts were measured. The two
committed fixtures exercise the intended boundary: a queue implementation with
module/path ownership and a traced service topology with partial attribution.

## Regeneration

From the repository root:

```bash
uv run python scripts/generate_architecture_graph_contract.py \
  --schema clients/libs/contracts/src/generated/architecture-graph.schema.json \
  --module clients/libs/contracts/src/generated/architecture-graph.schema.ts
pnpm --dir clients exec json2ts \
  -i libs/contracts/src/generated/architecture-graph.schema.json \
  -o libs/contracts/src/generated/architecture-graph.generated.ts \
  --bannerComment '/* Generated from vibesys.api.architecture. Do not edit. */'
```

Repository checks regenerate all three artifacts and fail on drift.
