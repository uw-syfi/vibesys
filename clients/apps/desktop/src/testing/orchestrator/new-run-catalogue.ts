/**
 * The fixture host's whole New run catalogue: the entries of `new-run-fixtures` plus cards derived
 * from real example tasks (CPU data structures and databases, and GPU model serving across vendors),
 * and its eval places plus one the host cannot read. The fit of every entry is `fitOf`, the host's rule.
 */
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {
  AcceleratorModel,
  AcceleratorVendor,
  type CatalogueEntry,
  CatalogueEntrySchema,
  type EvalPlace,
  EvalPlaceKind,
  EvalPlaceSchema,
  ListCatalogueResponseSchema,
  ListEvalPlacesResponseSchema,
  ModelMaker,
  OffersUnreadableReason,
  oneofIs,
  ProvisionRefusal,
  type ProvisionRepositoryRequest,
  ProvisionRepositoryResponseSchema,
  TaskCardSchema,
  TaskDomain,
  TaskObjectiveDirection,
} from '@vibesys/orchestrator-proto';
import {
  FIXTURE_REPOSITORIES_DIRECTORY,
  fitOf,
  fixtureCatalogueResponse,
  fixturePlaces,
  provisionAnswer,
} from './new-run-fixtures.js';

const card = (fields: MessageInitShape<typeof TaskCardSchema>) => create(TaskCardSchema, fields);

const objective = (name: string, direction: TaskObjectiveDirection, unit: string) => ({
  name,
  direction,
  unit,
});
const ops = objective('total_ops_per_sec', TaskObjectiveDirection.MAX, 'ops/s');
const latency = objective('latency_p50', TaskObjectiveDirection.MIN, 'ms');

const cpu = {vendor: AcceleratorVendor.CPU, model: AcceleratorModel.UNSPECIFIED, count: 1};

/** Entries from real example tasks: the example's path under `examples/`, the task, its card, and whether it clones. */
const REAL: ReadonlyArray<readonly [string, ReturnType<typeof card>, boolean]> = [
  [
    'model-serving/show-o2-1.5B-HQ',
    card({
      task: 'h100',
      domain: TaskDomain.LLM_SERVING,
      hardware: {vendor: AcceleratorVendor.NVIDIA, model: AcceleratorModel.H100, count: 1},
      hardwareName: 'NVIDIA H100',
      modelName: 'Show-o2 1.5B HQ',
      heading: 'Show-o2 1.5B HQ image generation server on H100',
      objectives: [latency],
    }),
    false,
  ],
  [
    'model-serving/qwen3.5-397b-a17b-4xb200',
    card({
      task: 'default',
      domain: TaskDomain.LLM_SERVING,
      hardware: {vendor: AcceleratorVendor.NVIDIA, model: AcceleratorModel.B200, count: 4},
      hardwareName: 'NVIDIA B200',
      modelName: 'Qwen3.5 397B A17B',
      modelMaker: ModelMaker.QWEN,
      heading: 'Qwen3.5 397B A17B serving on 4 B200 GPUs',
      objectives: [objective('output_tok_s', TaskObjectiveDirection.MAX, 'tok/s')],
    }),
    false,
  ],
  [
    'model-serving/Llama-3.1-8B-Instruct-MLX-8bit',
    card({
      task: 'default',
      domain: TaskDomain.LLM_SERVING,
      hardware: {
        vendor: AcceleratorVendor.APPLE,
        model: AcceleratorModel.APPLE_SILICON,
        count: 1,
      },
      hardwareName: 'Apple Silicon',
      modelName: 'Llama 3.1 8B Instruct',
      modelMaker: ModelMaker.META,
      heading: 'Llama-3.1-8B MLX 8-bit JSON-schema server',
      objectives: [latency],
    }),
    false,
  ],
  [
    'data-structures/prioqueue',
    card({
      task: 'mpmc',
      domain: TaskDomain.DATA_STRUCTURES,
      hardware: cpu,
      hardwareName: 'CPU',
      workloadName: 'Multiple producers, multiple consumers',
      workloadDescription: 'A bounded priority queue checked for linearizability.',
      heading: 'Optimize a multi-producer, multi-consumer bounded priority queue.',
      objectives: [ops],
    }),
    false,
  ],
  [
    'data-structures/prioqueue',
    card({
      task: 'spsc',
      domain: TaskDomain.DATA_STRUCTURES,
      hardware: cpu,
      hardwareName: 'CPU',
      workloadName: 'Single producer, single consumer',
      heading: 'Optimize a single-producer, single-consumer bounded priority queue.',
      objectives: [ops],
    }),
    false,
  ],
  [
    'data-structures/ordered-map',
    card({
      task: 'range-heavy',
      domain: TaskDomain.DATA_STRUCTURES,
      hardware: cpu,
      hardwareName: 'CPU',
      workloadName: 'Range-heavy ordered map',
      heading: 'Optimize a range-heavy concurrent ordered map.',
      objectives: [ops],
    }),
    false,
  ],
  [
    'database/differential-dataflow',
    card({
      task: 'default',
      domain: TaskDomain.DATABASES,
      hardware: cpu,
      hardwareName: 'CPU',
      workloadName: 'Differential dataflow',
      heading: 'Superoptimize differential-dataflow in place',
      objectives: [objective('cpu_seconds', TaskObjectiveDirection.MIN, 's')],
    }),
    true,
  ],
];

/** A place the host could not read: it fits nothing, and the select says so. */
const unreachable = (): EvalPlace =>
  create(EvalPlaceSchema, {
    name: 'cluster-b',
    kind: EvalPlaceKind.SLURM,
    state: {case: 'unreadable', value: {reason: OffersUnreadableReason.UNREACHABLE}},
  });

/** The fixture host's eval places: the ones of `fixturePlaces`, and one it cannot read. */
export const richPlaces = (): ReadonlyArray<EvalPlace> => [...fixturePlaces(), unreachable()];

export const richPlacesResponse = () =>
  create(ListEvalPlacesResponseSchema, {places: [...richPlaces()]});

/** The fixture host's catalogue: every entry with the host's fit over `richPlaces`. */
export function richCatalogueResponse() {
  const places = richPlaces();
  const base = fixtureCatalogueResponse();
  const entries: CatalogueEntry[] = [
    ...base.entries,
    ...REAL.map(([example, entryCard, needsClone]) =>
      create(CatalogueEntrySchema, {
        example,
        card: entryCard,
        needsClone,
        fit: fitOf(entryCard, places),
      }),
    ),
  ].map(entry =>
    create(CatalogueEntrySchema, {
      example: entry.example,
      card: entry.card,
      needsClone: entry.needsClone,
      fit: entry.card === undefined ? entry.fit : fitOf(entry.card, places),
    }),
  );
  return create(ListCatalogueResponseSchema, {
    release: base.release,
    repositoriesDirectory: FIXTURE_REPOSITORIES_DIRECTORY,
    entries,
  });
}

const HOME = '/home/user';
const READ_ONLY = ['/proc', '/sys', '/usr'];

const refused = (reason: ProvisionRefusal, detail: string, existing = '') =>
  create(ProvisionRepositoryResponseSchema, {
    outcome: {case: 'refused', value: {reason, detail, existingRepository: existing}},
  });

/**
 * The fixture host's answer to a provision over the whole catalogue: `~` in the parent is the home
 * folder, a read-only parent is refused, an example that clones is refused as needing the network,
 * and the rest is the answer of `provisionAnswer` (a name taken, an example it has none of).
 */
export function richProvisionAnswer(
  request: ProvisionRepositoryRequest,
  taken: ReadonlySet<string>,
) {
  const parent = request.parentDirectory.replace(/^~(?=\/|$)/, HOME);
  if (READ_ONLY.some(folder => parent === folder || parent.startsWith(`${folder}/`)))
    return refused(ProvisionRefusal.PARENT_NOT_WRITABLE, `${parent} is read-only`);
  const {source} = request;
  const real = REAL.find(
    ([example]) =>
      oneofIs(source, 'example', {example: true, empty: true}) && example === source.value,
  );
  if (real === undefined) return provisionAnswer({...request, parentDirectory: parent}, taken);
  const repository = `${parent === '' ? FIXTURE_REPOSITORIES_DIRECTORY : parent}/${request.name}`;
  if (taken.has(repository))
    return refused(ProvisionRefusal.NAME_TAKEN, `${request.name} exists`, repository);
  if (real[2])
    return refused(ProvisionRefusal.NEEDS_NETWORK, `${real[0]} is cloned from elsewhere`);
  return create(ProvisionRepositoryResponseSchema, {
    outcome: {
      case: 'provisioned',
      value: {repository, commit: '0123456789abcdef0123456789abcdef01234567'},
    },
  });
}
