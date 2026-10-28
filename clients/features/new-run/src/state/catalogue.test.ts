import {describe, expect, test} from 'bun:test';
import {exampleCatalogue, exampleCatalogueWithoutDetails} from '@vibesys/run-fixtures';
import {entriesOf, exampleLabel} from './catalogue.js';

const ENTRIES = entriesOf(exampleCatalogue);

describe('the example catalogue as New run reads it', () => {
  test('there is one entry per task, none for an example that was not fetched', () => {
    const tasks = exampleCatalogue.examples.flatMap(example => example.task_details ?? []);
    expect(ENTRIES.length).toBe(tasks.length);
    expect(ENTRIES.some(entry => entry.example === 'model-serving/repositories/vllm')).toBe(false);
    expect(ENTRIES.every(entry => entry.details !== null)).toBe(true);
  });

  test('a helper without the catalogue lists examples and their task names only', () => {
    const plain = entriesOf(exampleCatalogueWithoutDetails);
    expect(plain.every(entry => entry.details === null)).toBe(true);
    expect(plain.map(entry => entry.task)).toContain('vllm');
    expect(plain.map(entry => entry.example)).toContain('data-structures/stack');
  });

  test('an example is named by its own name, not its category directory', () => {
    expect(exampleLabel('data-structures/stack')).toBe('stack');
    expect(exampleLabel('stack')).toBe('stack');
  });
});
