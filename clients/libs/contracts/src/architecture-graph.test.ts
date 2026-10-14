import {test} from 'node:test';
import {BackendClientError} from './errors.js';
import {parseArchitectureGraph} from './index.js';
import {expect} from './test-support/expect.js';

const minimalGraph = {
  schema_version: 1,
  graph_id: 'fixture',
  title: 'Fixture graph',
  description: 'A graph small enough to expose every reference rule.',
  generation: {approach: 'static-analysis', source: 'Public test input.'},
  components: [
    {
      id: 'source',
      label: 'Source',
      kind: 'service',
      anchor: {kind: 'service', value: 'source'},
      description: 'Source component.',
      group: null,
    },
    {
      id: 'target',
      label: 'Target',
      kind: 'database',
      anchor: {kind: 'service', value: 'target'},
      description: 'Target component.',
      group: null,
    },
  ],
  edges: [
    {
      id: 'source-target',
      source_id: 'source',
      target_id: 'target',
      kind: 'storage',
      label: null,
    },
  ],
  observations: [
    {
      measurement_id: 'measurement-1',
      sequence: 1,
      label: 'Baseline',
      changes: [{component_id: 'target', changed_files: ['schema/data.sql']}],
      attributions: [{component_id: 'target', cost: null, latency: null}],
    },
  ],
} as const;

test('parses the generated graph contract through the public boundary', () => {
  const graph = parseArchitectureGraph(minimalGraph);
  expect(graph.components.map(component => component.id)).toEqual(['source', 'target']);
  expect(graph.observations[0]?.attributions[0]?.latency).toBeNull();
});

test('rejects unknown fields and dangling references with their contract path', () => {
  const unknown = clone();
  (unknown['generation'] as Record<string, unknown>)['confidence'] = 0.5;
  expect(() => parseArchitectureGraph(unknown)).toThrow('generation.confidence');

  const dangling = clone();
  ((dangling['edges'] as Record<string, unknown>[])[0] as Record<string, unknown>)['target_id'] =
    'missing';
  expect(() => parseArchitectureGraph(dangling)).toThrow(
    'edges[0].target_id: references unknown component',
  );

  const danglingChange = clone();
  const changes = (danglingChange['observations'] as Record<string, unknown>[])[0]?.[
    'changes'
  ] as Record<string, unknown>[];
  if (changes[0] !== undefined) changes[0]['component_id'] = 'missing';
  expect(() => parseArchitectureGraph(danglingChange)).toThrow(
    'observations[0].changes: references unknown component',
  );
});

test('rejects duplicate identities and non-monotone observations', () => {
  const duplicate = clone();
  (duplicate['components'] as Record<string, unknown>[])[1] = {
    ...(duplicate['components'] as Record<string, unknown>[])[1],
    id: 'source',
  };
  expect(() => parseArchitectureGraph(duplicate)).toThrow('components[1]: duplicates');

  const reversed = clone();
  const observation = (reversed['observations'] as Record<string, unknown>[])[0];
  (reversed['observations'] as Record<string, unknown>[]).push({
    ...observation,
    measurement_id: 'measurement-0',
    sequence: 0,
  });
  expect(() => parseArchitectureGraph(reversed)).toThrow(
    'observations[1].sequence: must be strictly greater than 1',
  );
});

test('rejects changed files that are not normalized relative paths', () => {
  for (const path of [
    '.',
    '../outside',
    '/absolute',
    'src//file.ts',
    'src\\file.ts',
    './file.ts',
  ]) {
    const invalid = clone();
    const observations = invalid['observations'] as Record<string, unknown>[];
    const changes = observations[0]?.['changes'] as Record<string, unknown>[];
    if (changes[0] !== undefined) changes[0]['changed_files'] = [path];

    expect(() => parseArchitectureGraph(invalid)).toThrow('must be a normalized relative path');
  }
});

test('contract errors remain typed parse failures', () => {
  expect(() => parseArchitectureGraph(null)).toThrow(BackendClientError);
});

function clone(): Record<string, unknown> {
  return JSON.parse(JSON.stringify(minimalGraph)) as Record<string, unknown>;
}
