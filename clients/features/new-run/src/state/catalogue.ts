/**
 * The example catalogue as New run reads it: one entry per (example, task), with what the helper
 * says of the task. It reads only the helper's contract (`ExampleList`), never an example's
 * directory name.
 *
 * A helper that predates the catalogue (feature `example_catalogue`) lists examples with their task
 * names and nothing else: those entries carry no `details`, and the pick lists them by name alone.
 */
import type {ExampleHardware, ExampleList, ExampleTask} from '@vibesys/contracts/host-helper';

export type Domain = ExampleTask['domain'];

export interface CatalogueEntry {
  readonly example: string;
  readonly category: string;
  readonly task: string;
  /** What the helper said of the task; null from a helper without `example_catalogue`. */
  readonly details: ExampleTask | null;
}

/** The domains in the order the pick lists them. */
export const DOMAINS: readonly Domain[] = [
  'llm-serving',
  'data-structures',
  'databases',
  'microservices',
];

/** The name each domain goes by. */
export const DOMAIN_WORDS: Readonly<Record<Domain, {readonly name: string}>> = {
  'llm-serving': {name: 'LLM serving'},
  'data-structures': {name: 'Data structures'},
  databases: {name: 'Databases'},
  microservices: {name: 'Microservices'},
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

/** The example's own name, without its category directory: `data-structures/stack` is `stack`. */
export function exampleLabel(example: string): string {
  return example.split('/').filter(Boolean).at(-1) ?? example;
}

/** The hardware as people say it: `2x NVIDIA H100`. */
export function hardwareLabel(hardware: ExampleHardware): string {
  return `${hardware.count}× ${hardware.accelerator}`;
}
