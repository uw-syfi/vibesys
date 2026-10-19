/**
 * The example catalogue as New run's questions read it: one entry per (example, task), the options
 * each question offers (domains, models by maker, hardware by vendor, structures, workloads,
 * examples) and which entry a set of answers names. It reads only the helper's contract
 * (`ExampleList`), never an example's directory name.
 *
 * A helper that predates the catalogue (feature `example_catalogue`) lists examples with their task
 * names and nothing else: those entries carry no `details`, and the flow falls back to choosing an
 * example and then its task.
 */
import type {
  ExampleHardware,
  ExampleList,
  ExampleModel,
  ExampleTask,
} from '@vibesys/contracts/host-helper';

export type Domain = ExampleTask['domain'];
export type Vendor = ExampleHardware['vendor'];
export type Maker = ExampleModel['maker'];

export interface CatalogueEntry {
  readonly example: string;
  readonly category: string;
  readonly task: string;
  /** What the helper said of the task; null from a helper without `example_catalogue`. */
  readonly details: ExampleTask | null;
}

/** The domains in the order the first question lists them. */
const DOMAINS: readonly Domain[] = ['llm-serving', 'data-structures', 'databases', 'microservices'];

/** The name each domain goes by. */
export const DOMAIN_WORDS: Readonly<Record<Domain, {readonly name: string}>> = {
  'llm-serving': {name: 'LLM serving'},
  'data-structures': {name: 'Data structures'},
  databases: {name: 'Databases'},
  microservices: {name: 'Microservices'},
};

/** The makers in the order their groups are listed; a maker the catalogue does not use is not shown. */
const MAKERS: readonly Maker[] = [
  'meta',
  'qwen',
  'deepseek',
  'zhipu',
  'moonshot',
  'ai2',
  'openai',
  'other',
];

export const MAKER_NAMES: Readonly<Record<Maker, string>> = {
  meta: 'Meta',
  qwen: 'Qwen',
  deepseek: 'DeepSeek',
  zhipu: 'Zhipu AI',
  moonshot: 'Moonshot AI',
  ai2: 'Ai2',
  openai: 'OpenAI',
  other: 'Other makers',
};

const VENDORS: readonly Vendor[] = ['nvidia', 'amd', 'aws', 'apple', 'cpu'];

export const VENDOR_NAMES: Readonly<Record<Vendor, string>> = {
  nvidia: 'NVIDIA',
  amd: 'AMD',
  aws: 'AWS',
  apple: 'Apple',
  cpu: 'CPU',
};

/** One entry per task of every example; an example with no listed tasks (not fetched) has none. */
export function entriesOf(list: ExampleList): CatalogueEntry[] {
  return list.examples.flatMap((example): CatalogueEntry[] => {
    const details = example.task_details;
    if (details === undefined || details === null) {
      return example.tasks.map(task => ({
        example: example.name,
        category: example.category,
        task,
        details: null,
      }));
    }
    return details.map(each => ({
      example: example.name,
      category: example.category,
      task: each.task,
      details: each,
    }));
  });
}

/** Whether every entry says what its task is about, so the questions can be asked by domain. */
export function hasDetails(entries: readonly CatalogueEntry[]): boolean {
  return entries.length > 0 && entries.every(entry => entry.details !== null);
}

/** The example's own name, without its category directory: `data-structures/stack` is `stack`. */
export function exampleLabel(example: string): string {
  return example.split('/').filter(Boolean).at(-1) ?? example;
}

/** The hardware a task targets, as one key: what two tasks share when they run on the same. */
export function hardwareKey(hardware: ExampleHardware): string {
  return `${hardware.vendor}|${hardware.accelerator}|${hardware.count}`;
}

/** The hardware as people say it: `2x NVIDIA H100`. */
export function hardwareLabel(hardware: ExampleHardware): string {
  return `${hardware.count}× ${hardware.accelerator}`;
}

function distinct<T>(values: readonly T[]): T[] {
  return [...new Set(values)];
}

export interface DomainOption {
  readonly domain: Domain;
  /** The examples of the domain. */
  readonly count: number;
}

/** The domains that have an example, in listing order. */
export function domainOptions(entries: readonly CatalogueEntry[]): DomainOption[] {
  return DOMAINS.flatMap(domain => {
    const count = distinct(
      entries.filter(entry => entry.details?.domain === domain).map(entry => entry.example),
    ).length;
    return count === 0 ? [] : [{domain, count}];
  });
}

export interface ModelOption {
  readonly name: string;
}

export interface MakerGroup {
  readonly maker: Maker;
  readonly models: readonly ModelOption[];
}

/**
 * The served models of the LLM-serving entries, grouped by maker, one option per distinct model:
 * the hardware a model has examples for is the next question's (`hardwareGroups`).
 */
export function modelGroups(entries: readonly CatalogueEntry[]): MakerGroup[] {
  const serving = entries.flatMap(entry =>
    entry.details?.domain === 'llm-serving' && entry.details.model !== null
      ? [entry.details.model]
      : [],
  );
  return MAKERS.flatMap(maker => {
    const names = distinct(serving.filter(model => model.maker === maker).map(model => model.name));
    return names.length === 0 ? [] : [{maker, models: names.map(name => ({name}))}];
  });
}

export interface HardwareOption {
  readonly key: string;
  readonly label: string;
  readonly backend: ExampleHardware['backend'];
}

export interface VendorGroup {
  readonly vendor: Vendor;
  readonly options: readonly HardwareOption[];
}

/** The hardware `model` has an example for, grouped by vendor: the choices of the Hardware question. */
export function hardwareGroups(entries: readonly CatalogueEntry[], model: string): VendorGroup[] {
  const targets = entries.flatMap(entry =>
    entry.details?.domain === 'llm-serving' && entry.details.model?.name === model
      ? [entry.details.hardware]
      : [],
  );
  return VENDORS.flatMap(vendor => {
    const ofVendor = targets.filter(hardware => hardware.vendor === vendor);
    const keys = distinct(ofVendor.map(hardwareKey));
    if (keys.length === 0) return [];
    return [
      {
        vendor,
        options: keys.map(key => {
          const hardware = ofVendor.find(each => hardwareKey(each) === key) as ExampleHardware;
          return {key, label: hardwareLabel(hardware), backend: hardware.backend};
        }),
      },
    ];
  });
}

/** The entries of `model` that run on the hardware `key`. */
export function entriesFor(
  entries: readonly CatalogueEntry[],
  model: string,
  key: string,
): CatalogueEntry[] {
  return entries.filter(
    entry =>
      entry.details?.domain === 'llm-serving' &&
      entry.details.model?.name === model &&
      hardwareKey(entry.details.hardware) === key,
  );
}

/** The distinct examples among `entries` of `domain`, in listing order. */
export function examplesOf(entries: readonly CatalogueEntry[], domain: Domain): string[] {
  return distinct(entries.filter(entry => entry.details?.domain === domain).map(e => e.example));
}

/** Every distinct example, for a helper that cannot say what its examples are about. */
export function allExamples(entries: readonly CatalogueEntry[]): string[] {
  return distinct(entries.map(entry => entry.example));
}

/** The entries of one example. */
export function tasksOfExample(
  entries: readonly CatalogueEntry[],
  example: string,
): CatalogueEntry[] {
  return entries.filter(entry => entry.example === example);
}
