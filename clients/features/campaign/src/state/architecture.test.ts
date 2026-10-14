import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {parseArchitectureGraph} from '@vibesys/contracts';
import {
  foldArchitectureCursor,
  initialArchitectureState,
  layoutArchitecture,
  projectArchitecture,
} from './index.js';

const queue = fixture('architecture-queue.json');
const microservices = fixture('architecture-microservices.json');

test('the campaign cursor selects the latest prefix without accumulating later changes', () => {
  const initial = initialArchitectureState(queue);
  expect(projectArchitecture(initial).observation).toBeNull();

  const second = foldArchitectureCursor(initial, {
    type: 'campaign-cursor',
    cursor: {measurementId: 'queue-copy-elision', sequence: 2},
  });
  expect([...projectArchitecture(second).changedComponentIds]).toEqual([
    'queue.producer',
    'queue.deque',
  ]);
  expect(projectArchitecture(second).changedFiles).toEqual(['src/lib.rs']);

  const third = foldArchitectureCursor(second, {
    type: 'campaign-cursor',
    cursor: {measurementId: 'queue-ffi-bounds', sequence: 3},
  });
  const view = projectArchitecture(third);
  expect([...view.changedComponentIds]).toEqual(['queue.ffi']);
  expect(view.changedFiles).toEqual(['src/ffi.rs']);
  expect(view.attributions.size).toBe(0);
});

test('a cursor between captures keeps the last recorded architecture observation', () => {
  const state = foldArchitectureCursor(initialArchitectureState(microservices), {
    type: 'campaign-cursor',
    cursor: {measurementId: 'campaign-measurement-without-capture', sequence: 25},
  });
  const view = projectArchitecture(state);
  expect(view.observation?.measurement_id).toBe('hotel-pooled-client');
  expect(view.attributions.get('hotel.frontend')?.latency?.value).toBe(15.2);
  expect([...view.changedComponentIds]).toEqual([]);
  expect(view.changedFiles).toEqual([]);
});

test('replaying an unchanged campaign cursor preserves state identity', () => {
  const state = foldArchitectureCursor(initialArchitectureState(queue), {
    type: 'campaign-cursor',
    cursor: {measurementId: 'queue-baseline', sequence: 1},
  });
  expect(
    foldArchitectureCursor(state, {
      type: 'campaign-cursor',
      cursor: {measurementId: 'queue-baseline', sequence: 1},
    }),
  ).toBe(state);
});

test('layout is deterministic across component and edge input order', () => {
  const forward = layoutArchitecture(queue);
  const reversedComponents = [...queue.components].reverse();
  const first = reversedComponents.shift();
  if (first === undefined) throw new Error('fixture must contain a component');
  const reverse = layoutArchitecture({
    ...queue,
    components: [first, ...reversedComponents],
    edges: [...queue.edges].reverse(),
  });
  expect(reverse).toEqual(forward);
});

function fixture(name: string) {
  const path = resolve(import.meta.dir, `../fixtures/${name}`);
  return parseArchitectureGraph(JSON.parse(readFileSync(path, 'utf8')));
}
