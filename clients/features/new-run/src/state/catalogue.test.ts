import {describe, expect, test} from 'bun:test';
import {exampleCatalogue, exampleCatalogueWithoutDetails} from '@vibesys/run-fixtures';
import {
  allExamples,
  domainOptions,
  entriesFor,
  entriesOf,
  exampleLabel,
  hardwareGroups,
  hardwareKey,
  hasDetails,
  modelGroups,
} from './catalogue.js';

const ENTRIES = entriesOf(exampleCatalogue);

describe('the example catalogue as the questions read it', () => {
  test('there is one entry per task, none for an example that was not fetched', () => {
    const tasks = exampleCatalogue.examples.flatMap(example => example.task_details ?? []);
    expect(ENTRIES.length).toBe(tasks.length);
    expect(ENTRIES.some(entry => entry.example === 'model-serving/repositories/vllm')).toBe(false);
    expect(hasDetails(ENTRIES)).toBe(true);
  });

  test('a helper without the catalogue lists examples and their task names only', () => {
    const plain = entriesOf(exampleCatalogueWithoutDetails);
    expect(hasDetails(plain)).toBe(false);
    expect(plain.every(entry => entry.details === null)).toBe(true);
    expect(plain.map(entry => entry.task)).toContain('vllm');
    expect(allExamples(plain)).toContain('data-structures/stack');
  });

  test('the domains offered are the ones with an example, with how many', () => {
    const offered = domainOptions(ENTRIES);
    expect(offered.map(option => option.domain)).toEqual([
      'llm-serving',
      'data-structures',
      'databases',
      'microservices',
    ]);
    expect(offered.find(option => option.domain === 'data-structures')?.count).toBe(1);
  });

  test('models are grouped by maker with the hardware each has an example for', () => {
    const groups = modelGroups(ENTRIES);
    const meta = groups.find(group => group.maker === 'meta');
    expect(meta?.models.map(model => model.name)).toContain('Llama 3 8B Instruct');
    const twice = entriesOf({
      ...exampleCatalogue,
      examples: exampleCatalogue.examples.flatMap(example => {
        const details = example.task_details;
        if (
          details === undefined ||
          details === null ||
          example.name !== 'model-serving/Llama-3-8B-trn2'
        ) {
          return [];
        }
        // The same model served on a second kind of hardware.
        return [
          example,
          {
            ...example,
            name: 'model-serving/Llama-3-8B-mi210',
            task_details: details.map(each => ({
              ...each,
              hardware: {
                accelerator: 'AMD MI210',
                count: 1,
                vendor: 'amd' as const,
                backend: 'rocm' as const,
              },
            })),
          },
        ];
      }),
    });
    expect(modelGroups(twice)[0]?.models[0]?.hardware).toEqual([
      '1\u00d7 AWS Trainium2',
      '1\u00d7 AMD MI210',
    ]);
    // Each model appears once, under one maker.
    const names = groups.flatMap(group => group.models.map(model => model.name));
    expect(new Set(names).size).toBe(names.length);
  });

  test('every hardware the catalogue targets is offered once, by vendor, even where a model has none', () => {
    const groups = hardwareGroups(ENTRIES);
    const keys = groups.flatMap(group => group.options.map(option => option.key));
    expect(new Set(keys).size).toBe(keys.length);
    for (const entry of ENTRIES) {
      if (entry.details?.domain !== 'llm-serving') continue;
      expect(keys).toContain(hardwareKey(entry.details.hardware));
    }
    const model = 'Qwen3.5 9B';
    const has = keys.filter(key => entriesFor(ENTRIES, model, key).length > 0);
    expect(has.length).toBeLessThan(keys.length);
    expect(has.length).toBeGreaterThan(0);
  });

  test('an example is named by its own name, not its category directory', () => {
    expect(exampleLabel('data-structures/stack')).toBe('stack');
    expect(exampleLabel('stack')).toBe('stack');
  });
});
