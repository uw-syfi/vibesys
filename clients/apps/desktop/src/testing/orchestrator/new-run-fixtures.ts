/**
 * The facts the fake host answers the New run calls with: eval places, a catalogue of example tasks
 * with their fit, and the outcome of a provision. Each is built through its generated schema, so a
 * field the contract grows is a type error here. The fit is worked out from the places by the same
 * rule the real host states: the vendor matches, the place has at least the count, and the model
 * matches unless the task names none.
 */
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {
  AcceleratorModel,
  AcceleratorVendor,
  type CatalogueEntry,
  CatalogueEntrySchema,
  type CatalogueProblemReason,
  CatalogueProblemSchema,
  type EvalPlace,
  EvalPlaceKind,
  EvalPlaceSchema,
  type Fit,
  FitSchema,
  FitState,
  ListCatalogueResponseSchema,
  ModelMaker,
  OffersSource,
  oneofIs,
  ProvisionRefusal,
  type ProvisionRepositoryRequest,
  ProvisionRepositoryResponseSchema,
  type TaskCard,
  TaskCardSchema,
  TaskDomain,
  TaskObjectiveDirection,
} from '@vibesys/orchestrator-proto';

/** Where the fake host puts a new repository when the call names no parent. */
export const FIXTURE_REPOSITORIES_DIRECTORY = '/home/user/.vibesys/repos';

export const offer = (
  model: AcceleratorModel,
  vendor: AcceleratorVendor,
  count: number,
  name: string,
) => ({
  vendor,
  model,
  count,
  deviceName: name,
});

export const place = (
  name: string,
  kind: EvalPlaceKind,
  offers: ReadonlyArray<ReturnType<typeof offer>>,
): EvalPlace =>
  create(EvalPlaceSchema, {
    name,
    kind,
    offers: [...offers],
    state: {case: 'known', value: {source: OffersSource.PROBED}},
  });

/** The places of the fake host: the machine's own Docker, and two remote places with accelerators. */
export const fixturePlaces = (): ReadonlyArray<EvalPlace> => [
  place('local', EvalPlaceKind.LOCAL, []),
  place('host-a', EvalPlaceKind.SSH, [
    offer(AcceleratorModel.H100, AcceleratorVendor.NVIDIA, 8, 'NVIDIA H100 80GB HBM3'),
  ]),
  place('cluster-a', EvalPlaceKind.SLURM, [
    offer(AcceleratorModel.MI300X, AcceleratorVendor.AMD, 8, 'AMD Instinct MI300X'),
  ]),
];

/** Whether any place gives the card's accelerator, and which: the host's fit rule. */
export function fitOf(card: TaskCard, places: ReadonlyArray<EvalPlace>): Fit {
  const need = card.hardware;
  // A place whose hardware is unreadable fits nothing, a CPU need included. With no place that can
  // be read, and none at all is that case, the answer is PLACES_UNREADABLE whatever the need.
  const readable = places.filter(each =>
    oneofIs(each.state, 'known', {known: true, unreadable: true}),
  );
  if (readable.length === 0) return create(FitSchema, {state: FitState.PLACES_UNREADABLE});
  if (need === undefined || need.vendor === AcceleratorVendor.CPU)
    return create(FitSchema, {state: FitState.FITS, places: readable.map(each => each.name)});
  if (need.model === AcceleratorModel.OTHER)
    return create(FitSchema, {state: FitState.UNKNOWN_ACCELERATOR});
  const fitting = readable.filter(each =>
    each.offers.some(
      given =>
        given.model !== AcceleratorModel.OTHER &&
        given.vendor === need.vendor &&
        given.count >= need.count &&
        (need.model === AcceleratorModel.UNSPECIFIED || given.model === need.model),
    ),
  );
  return create(FitSchema, {
    state: fitting.length > 0 ? FitState.FITS : FitState.NO_ACCELERATOR,
    places: fitting.map(each => each.name),
  });
}

const card = (fields: MessageInitShape<typeof TaskCardSchema>) => create(TaskCardSchema, fields);

const throughput = {name: 'throughput', direction: TaskObjectiveDirection.MAX, unit: 'tok/s'};
const latency = {name: 'latency', direction: TaskObjectiveDirection.MIN, unit: 'ms'};
const ops = {name: 'ops_per_sec', direction: TaskObjectiveDirection.MAX, unit: 'ops/s'};

/** The example tasks of the fake host, as cards: one or two of each domain. */
const fixtureCards: ReadonlyArray<readonly [string, TaskCard, boolean]> = [
  [
    'model-serving/flint',
    card({
      task: 'flint',
      domain: TaskDomain.LLM_SERVING,
      hardware: {vendor: AcceleratorVendor.AMD, model: AcceleratorModel.MI300X, count: 8},
      hardwareName: 'AMD MI300X',
      modelName: 'Llama 3.3 70B',
      modelMaker: ModelMaker.META,
      heading: 'Serve Llama 3.3 70B faster',
      objectives: [throughput, latency],
    }),
    false,
  ],
  [
    'model-serving/ember',
    card({
      task: 'ember',
      domain: TaskDomain.LLM_SERVING,
      hardware: {vendor: AcceleratorVendor.NVIDIA, model: AcceleratorModel.UNSPECIFIED, count: 1},
      hardwareName: 'NVIDIA GPU',
      modelName: 'Qwen3 32B',
      modelMaker: ModelMaker.QWEN,
      heading: 'Serve Qwen3 32B faster',
      objectives: [throughput],
    }),
    true,
  ],
  [
    'model-serving/sparrow',
    card({
      task: 'sparrow',
      domain: TaskDomain.LLM_SERVING,
      hardware: {vendor: AcceleratorVendor.AWS, model: AcceleratorModel.TRAINIUM2, count: 1},
      hardwareName: 'AWS Trainium2',
      modelName: 'Llama 3.1 8B',
      modelMaker: ModelMaker.META,
      heading: 'Serve Llama 3.1 8B on Trainium2',
      objectives: [throughput],
    }),
    false,
  ],
  [
    'data-structures/swmr-map',
    card({
      task: 'swmr-map',
      domain: TaskDomain.DATA_STRUCTURES,
      hardware: {vendor: AcceleratorVendor.CPU, model: AcceleratorModel.UNSPECIFIED, count: 1},
      hardwareName: 'CPU',
      workloadName: 'Single writer, multiple readers',
      workloadDescription: 'A concurrent unordered map checked for linearizability.',
      heading: 'A faster concurrent map',
      objectives: [ops],
    }),
    false,
  ],
  [
    'databases/kv-store',
    card({
      task: 'kv-store',
      domain: TaskDomain.DATABASES,
      hardware: {vendor: AcceleratorVendor.CPU, model: AcceleratorModel.UNSPECIFIED, count: 1},
      hardwareName: 'CPU',
      heading: 'A faster key-value store',
      objectives: [ops],
    }),
    false,
  ],
];

export const fixtureCatalogueResponse = () =>
  create(ListCatalogueResponseSchema, {
    release: 'fixture-release',
    repositoriesDirectory: FIXTURE_REPOSITORIES_DIRECTORY,
    entries: fixtureCards.map(
      ([example, entryCard, needsClone]): CatalogueEntry =>
        create(CatalogueEntrySchema, {
          example,
          card: entryCard,
          needsClone,
          fit: fitOf(entryCard, fixturePlaces()),
        }),
    ),
  });

/**
 * The fake host's answer to `ListCatalogue` when it names a classified problem instead of entries:
 * still a successful reply, still naming its repositories directory, as the real host's does.
 */
export const fixtureCatalogueProblemResponse = (reason: CatalogueProblemReason, detail: string) =>
  create(ListCatalogueResponseSchema, {
    repositoriesDirectory: FIXTURE_REPOSITORIES_DIRECTORY,
    problem: create(CatalogueProblemSchema, {reason, detail}),
  });

/**
 * The fake host's answer to a provision: a repository under the named parent (or the default one),
 * or a refusal for a name it holds, an example it has none of, or an example that needs a clone.
 */
export function provisionAnswer(request: ProvisionRepositoryRequest, taken: ReadonlySet<string>) {
  const parent =
    request.parentDirectory === '' ? FIXTURE_REPOSITORIES_DIRECTORY : request.parentDirectory;
  const repository = `${parent}/${request.name}`;
  const refuse = (reason: ProvisionRefusal, detail: string, existing = '') =>
    create(ProvisionRepositoryResponseSchema, {
      outcome: {case: 'refused', value: {reason, detail, existingRepository: existing}},
    });
  if (taken.has(repository))
    return refuse(ProvisionRefusal.NAME_TAKEN, `${request.name} exists`, repository);
  const {source} = request;
  if (oneofIs(source, 'example', {example: true, empty: true})) {
    const entry = fixtureCards.find(([example]) => example === source.value);
    if (entry === undefined)
      return refuse(ProvisionRefusal.EXAMPLE_NOT_FOUND, `no example ${source.value}`);
    if (entry[2])
      return refuse(ProvisionRefusal.NEEDS_NETWORK, `${entry[0]} is cloned from elsewhere`);
  }
  return create(ProvisionRepositoryResponseSchema, {
    outcome: {
      case: 'provisioned',
      value: {repository, commit: '0123456789abcdef0123456789abcdef01234567'},
    },
  });
}
