/**
 * The input gates' row in a browser DOM, over the gates a run folded: a gate that failed stands
 * failed with its whole reason, and a gate a later attempt passed reads passed, its failed attempts
 * kept as history.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import type {RunModel} from '../../state/index.js';
import {foldInputGates, NO_INPUT_GATES} from '../../state/input-gates.js';
import {GateFailures} from './gate-failures.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));

beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});

afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

const REASON = `RuntimeError: ${'the engine refused to start '.repeat(6)}`.trim();

function gate(sequence: number, status: 'failed' | 'completed'): RunEvent {
  return {
    protocol_version: 1,
    run_id: 'run',
    sequence,
    timestamp: '2026-10-19T07:29:48Z',
    type: 'gate_finished',
    text: '',
    status,
    data: {
      kind: 'gate_finished',
      gate: 'benchmark',
      recipe: null,
      output_tail: status === 'failed' ? REASON : null,
    },
  } as RunEvent;
}

async function states(events: readonly RunEvent[]): Promise<{states: string[]; text: string}> {
  const inputGates = events.reduce(foldInputGates, NO_INPUT_GATES).failures;
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<GateFailures model={{inputGates} as unknown as RunModel} />));
  const marks = [...container.querySelectorAll('.vs-check')].map(
    each => each.getAttribute('data-state') ?? '',
  );
  const text = container.textContent ?? '';
  await act(async () => root.unmount());
  container.remove();
  return {states: marks, text};
}

test('a gate no attempt passed stands failed, with its whole reason', async () => {
  const {states: marks, text} = await states([gate(1, 'failed'), gate(2, 'failed')]);
  expect(marks).toEqual(['fail']);
  expect(text).toContain(REASON);
  expect(text).not.toContain('…');
});

test('a gate a later attempt passed reads passed and keeps its failed attempts as history', async () => {
  const {states: marks, text} = await states([
    gate(1, 'failed'),
    gate(2, 'failed'),
    gate(3, 'completed'),
  ]);
  expect(marks).toEqual(['ok']);
  expect(text).toContain('Benchmark');
  expect(text).toContain('2 failed');
  expect(text).toContain(REASON);
});

test('draws nothing when no gate failed', async () => {
  expect((await states([gate(1, 'completed')])).states).toEqual([]);
});
