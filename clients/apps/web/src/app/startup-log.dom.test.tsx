/**
 * The live page's startup log panel, composed over a session the test feeds run events to and a
 * Fake `RunLogSource` whose answers the test chooses: the panel follows the run's startup, and
 * goes away once the run is ready.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {event, eventBatch, snapshotResponse} from '@vibesys/contracts/testing';
import type {LogListOutcome, LogReadInput, LogReadOutcome, RunLogSource} from '@vibesys/logs';
import {FakeLifecycle, sessionWith} from '@vibesys/run-session/testing';
import type {AbortSignalLike} from '@vibesys/transport';
import {Window} from 'happy-dom';
import {act} from 'react';
import type {Root} from 'react-dom/client';
import {createLiveApp} from './compose.js';
import {createWebThemeController} from './theme.js';

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

async function settle(): Promise<void> {
  for (let turn = 0; turn < 50; turn += 1) await Promise.resolve();
}

function fakeTheme(): ReturnType<typeof createWebThemeController> {
  return createWebThemeController(
    {dataset: {}, style: {setProperty: () => undefined}},
    {read: () => null, save: () => undefined, remove: () => undefined},
  );
}

/** Lists one startup stream and answers its tail; later reads wait until their signal aborts. */
class FakeLogSource implements RunLogSource {
  readonly reads: {readonly input: LogReadInput; readonly signal: AbortSignalLike}[] = [];

  async list(): Promise<LogListOutcome> {
    return {
      ok: true,
      instanceId: '0123456789ab',
      streams: [
        {
          id: '000001',
          owner: {kind: 'startup_step', stage: 'building_image'},
          command: 'docker build',
          retained_start: 0,
          size: 30,
          outcome: null,
        },
      ],
    };
  }

  read(input: LogReadInput, signal: AbortSignalLike): Promise<LogReadOutcome> {
    this.reads.push({input, signal});
    if ('offset' in input.start) return new Promise(() => {});
    const data = 'building image 7/8: WORKDIR\n';
    return Promise.resolve({
      ok: true,
      chunk: {
        offset: 0,
        data,
        next_offset: data.length,
        eof: true,
        closed: false,
        truncated_before: false,
        pipes: [{offset: 0, length: data.length, pipe: 'stdout'}],
      },
    });
  }
}

function status(sequence: number, to: 'running', from: 'starting') {
  return event(sequence, 'run_status_changed', {
    data: {kind: 'run_status_changed', status: to, previous: from},
  });
}

test('a starting run shows its startup log, which goes away once the run is ready', async () => {
  const {session, transport} = sessionWith(new FakeLifecycle());
  transport.snapshots.push(snapshotResponse({sequence: 0, status: 'starting'}));
  transport.silentDials = 1;
  const logs = new FakeLogSource();
  const {createRoot} = await import('react-dom/client');
  const container = document.createElement('div');
  document.body.append(container);
  const root: Root = createRoot(container);
  await act(async () => root.render(createLiveApp(session, fakeTheme(), logs)));
  await settle();
  const subscription = transport.subscriptions[0];
  if (subscription === undefined) throw new Error('the session did not subscribe');

  await act(async () => {
    subscription.onMessage(
      eventBatch([], {store_id: 'store-1', through_sequence: 0, history_after_sequence: 0}),
    );
  });
  await act(settle);
  const panel = container.querySelector('[data-testid="startup-log"]');
  expect(panel?.querySelector('pre')?.textContent).toBe('building image 7/8: WORKDIR');
  const following = logs.reads.at(-1);
  expect(following?.input.start).toEqual({offset: 28});

  await act(async () => {
    subscription.onMessage(
      eventBatch([status(1, 'running', 'starting')], {
        store_id: 'store-1',
        through_sequence: 1,
        history_after_sequence: 0,
      }),
    );
  });
  await act(settle);
  expect(container.querySelector('[data-testid="startup-log"]')).toBeNull();
  expect(following?.signal.aborted).toBe(true);

  await act(async () => root.unmount());
  container.remove();
  await session.close();
});

test('the browser page, which has no log source, shows no startup log', async () => {
  const {session, transport} = sessionWith(new FakeLifecycle());
  transport.snapshots.push(snapshotResponse({sequence: 0, status: 'starting'}));
  transport.silentDials = 1;
  const {createRoot} = await import('react-dom/client');
  const container = document.createElement('div');
  document.body.append(container);
  const root: Root = createRoot(container);
  await act(async () => root.render(createLiveApp(session, fakeTheme())));
  await settle();
  await act(async () => {
    transport.subscriptions[0]?.onMessage(
      eventBatch([], {store_id: 'store-1', through_sequence: 0, history_after_sequence: 0}),
    );
  });
  await act(settle);
  expect(container.querySelector('[data-testid="startup-log"]')).toBeNull();
  await act(async () => root.unmount());
  container.remove();
  await session.close();
});
