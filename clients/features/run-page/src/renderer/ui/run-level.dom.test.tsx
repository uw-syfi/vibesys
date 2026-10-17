/**
 * The run level's faces in a DOM while a live run attaches: they say the run is being read until
 * its first snapshot is folded, and only then that it has no ideas or agents yet. Regression:
 * attaching to a live run flashed "No ideas yet" until the snapshot arrived.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {HostRun} from '@vibesys/hosts';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  createRunReader,
  type RunMode,
  type RunPageAction,
  type RunPageRequest,
  type RunPageState,
  runPageInitial,
  runPageStep,
} from '../../state/index.js';
import {RunLevel} from './run-level.js';

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

const RUNNING: HostRun = {
  id: 'a1b2c3d4e5f6',
  projectRoot: '~/vibesys-projects/queue-rs',
  runId: 'run-1',
  status: 'running',
  phase: 'running',
  paused: null,
  end: null,
  startedAt: 1,
  reason: null,
};

/** A live run's page attached and waiting for its first snapshot. */
function attaching(): RunPageState {
  let state = runPageInitial({
    identity: {host: 'ssh:chelan4', runId: 'run-1'},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'chelan4',
    unavailable: {},
  });
  const asked: RunPageRequest[] = [];
  const step = (action: RunPageAction): void => {
    const next = runPageStep(state, action);
    state = next.state;
    asked.push(...next.requests);
  };
  step({type: 'record', record: {kind: 'live', run: RUNNING}});
  const attach = asked.find(request => request.kind === 'attach');
  if (attach?.kind !== 'attach') throw new Error('no attach asked');
  step({type: 'attached', token: attach.token, error: null});
  return state;
}

const SNAPSHOT: RunPageAction = {
  type: 'session',
  session: {hasSnapshot: true, streaming: true, controls: true, error: null},
};

async function face(state: RunPageState, mode: RunMode): Promise<string> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const model = createRunReader().read(state.run.trace, null);
  const base = {model, clock: {start: 0, now: null}, zoom: () => undefined};
  await act(async () =>
    root.render(<RunLevel state={state} base={base} mode={mode} dispatch={() => undefined} />),
  );
  const text = container.textContent ?? '';
  await act(async () => root.unmount());
  container.remove();
  return text;
}

test('a face reads the run until the first snapshot, and only then says it is empty', async () => {
  const before = attaching();
  const after = runPageStep(before, SNAPSHOT).state;
  for (const [mode, empty] of [
    ['ideas', 'No ideas yet'],
    ['timeline', 'No ideas yet'],
    ['agents', 'No agents yet'],
  ] as const) {
    const waiting = await face(before, mode);
    expect(waiting).toContain('Reading the run…');
    expect(waiting).not.toContain(empty);
    const read = await face(after, mode);
    expect(read).toContain(empty);
    expect(read).not.toContain('Reading the run…');
  }
});
