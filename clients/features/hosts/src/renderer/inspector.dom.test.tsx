/**
 * The Inspect sheet over its store, in a browser DOM, with a fake welcome bridge: the run's log
 * streams listed by owner, the one on screen followed, narration and raw output rendered, and no
 * server log asked for.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {LogChunk, LogStream} from '@vibesys/contracts/host-helper';
import {createInspectorStore, InspectorSheet} from '@vibesys/feature-hosts/renderer';
import type {
  HostsBridge,
  InspectorSheetAction,
  InspectorTarget,
} from '@vibesys/feature-hosts/state';
import type {HostKey} from '@vibesys/hosts';
import type {RunLogsRequest, WelcomeInspection, WelcomeResult} from '@vibesys/ipc';
import type {LogListOutcome, LogReadOutcome} from '@vibesys/logs';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';

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

const INSTANCE = '0123456789ab';

const TARGET: InspectorTarget = {
  host: 'local',
  hostLabel: 'This Mac',
  instanceId: INSTANCE,
  task: 'queue',
  runId: 'run-1',
  project: '/projects/queue',
  focusFallbackId: 'recent-section',
};

const STREAMS: readonly LogStream[] = [
  {
    id: '000001',
    owner: {kind: 'startup_step', stage: 'building_image'},
    command: 'docker build',
    retained_start: 0,
    size: 0,
    outcome: 'failed',
  },
  {
    id: '000002',
    owner: {kind: 'run_narration'},
    command: 'vibesys',
    retained_start: 0,
    size: 0,
    outcome: null,
  },
];

const NARRATION = [
  {
    at: '2026-10-15T12:00:00Z',
    level: 'INFO',
    logger: 'vibesys.run',
    thread: 'MainThread',
    message: 'round 1 started',
    traceback: null,
  },
  {
    at: '2026-10-15T12:00:05Z',
    level: 'ERROR',
    logger: 'vibesys.gate',
    thread: 'MainThread',
    message: 'gate crashed',
    traceback: 'Traceback (most recent call last):\nValueError: boom',
  },
]
  .map(line => `${JSON.stringify(line)}\n`)
  .join('')
  .concat('not a record\n');

const encoder = new TextEncoder();

function chunk(data: string, pipes: LogChunk['pipes']): LogChunk {
  return {
    offset: 0,
    data,
    next_offset: encoder.encode(data).length,
    eof: true,
    closed: false,
    truncated_before: false,
    pipes,
  };
}

/** `step 1` on stdout, then `no space left` on stderr. */
const STARTUP = chunk('step 1\nno space left\n', [
  {offset: 0, length: 7, pipe: 'stdout'},
  {offset: 7, length: 14, pipe: 'stderr'},
]);

function inspection(): WelcomeInspection {
  return {
    version: 1,
    id: INSTANCE,
    state: 'ended',
    record: null,
    metadata: null,
    control_socket_path: '/run/control.sock',
    server_log_path: '/run/server.log',
    log: null,
  };
}

function ok<T>(value: T): WelcomeResult<T> {
  return {ok: true, value};
}

/**
 * The welcome bridge's hosts requests: inspections answer at once, a listing names `STREAMS`, a
 * tail read answers its stream's bytes, a read that follows waits until the test ends it, and a
 * withdrawal is recorded by the token it names.
 */
class FakeHostsBridge implements HostsBridge {
  readonly inspections: unknown[][] = [];
  readonly reads: {readonly host: HostKey; readonly stream: string; readonly tail: boolean}[] = [];
  /** The token of each read that follows, in order. */
  readonly following: string[] = [];
  readonly withdrawals: string[] = [];
  releases = 0;

  hosts = async () => ok<string[]>([]);
  host = async () => ok({key: 'local' as const, label: 'This Mac', checkout: '/src'});
  setCheckout = async () => ok('/src');
  cancelCheckout = async () => ok(null);
  cancelInspection = async () => ok(null);
  signIn = async () => ok(null);
  complete = async () =>
    ok({kind: 'listed' as const, directory: '/', entries: [], truncated: false});
  inspect = async (...args: unknown[]) => {
    this.inspections.push(args);
    return ok(inspection());
  };
  logs = (host: HostKey, instance: string, request: RunLogsRequest) => {
    if (request.kind === 'withdraw') {
      this.withdrawals.push(request.token);
      return Promise.resolve(ok<LogListOutcome | LogReadOutcome | null>(null));
    }
    if (request.kind === 'list') {
      const listing: LogListOutcome = {ok: true, instanceId: instance, streams: STREAMS};
      return Promise.resolve(ok<LogListOutcome | LogReadOutcome>(listing));
    }
    const tail = 'tail_bytes' in request.read.start;
    this.reads.push({host, stream: request.read.stream_id, tail});
    if (!tail) {
      this.following.push(request.token);
      return new Promise<WelcomeResult<LogListOutcome | LogReadOutcome>>(() => {});
    }
    const data =
      request.read.stream_id === '000002'
        ? chunk(NARRATION, [{offset: 0, length: encoder.encode(NARRATION).length, pipe: 'stdout'}])
        : STARTUP;
    return Promise.resolve(ok<LogListOutcome | LogReadOutcome>({ok: true, chunk: data}));
  };
  releaseLogs = async () => {
    this.releases += 1;
    return ok(null);
  };
}

async function settle(): Promise<void> {
  for (let round = 0; round < 10; round += 1) await act(async () => Promise.resolve());
}

async function mountSheet(bridge: FakeHostsBridge) {
  const store = createInspectorStore(bridge, async () => {}, new FakeClock().scheduleTimeout);
  const {createRoot} = await import('react-dom/client');
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const render = (): void =>
    root.render(<InspectorSheet state={store.getState()} send={store.dispatch} />);
  store.subscribe(render);
  await act(async () => render());
  const send = async (action: InspectorSheetAction): Promise<void> => {
    await act(async () => store.dispatch(action));
    await settle();
  };
  return {container, root, send};
}

test('the sheet lists the run logs by owner and follows the narration, level by level', async () => {
  const bridge = new FakeHostsBridge();
  const {container, root, send} = await mountSheet(bridge);

  await send({type: 'open', target: TARGET});

  const logs = container.ownerDocument.querySelector('[data-testid="inspection-logs"]');
  if (logs === null) throw new Error('the sheet has no logs section');
  const select = logs.querySelector('select');
  expect([...(select?.querySelectorAll('optgroup') ?? [])].map(group => group.label)).toEqual([
    'Run narration',
    'Startup',
  ]);
  expect([...(select?.querySelectorAll('option') ?? [])].map(option => option.textContent)).toEqual(
    ['Run narration (running)', 'Startup / building image (failed)'],
  );
  expect(select?.value).toBe('000002');
  // Only the stream on screen is read: its tail, then one read in flight that follows it.
  expect(bridge.reads).toEqual([
    {host: 'local', stream: '000002', tail: true},
    {host: 'local', stream: '000002', tail: false},
  ]);

  const records = [...logs.querySelectorAll('.log-narration > li')];
  expect(records.map(record => record.getAttribute('data-level'))).toEqual(['INFO', 'ERROR', null]);
  const error = records[1];
  expect(error?.className).toContain('level-error');
  expect(error?.querySelector('.narration-message')?.textContent).toBe('gate crashed');
  expect(error?.querySelector('details summary')?.textContent).toBe('Traceback');
  expect(error?.querySelector('details pre')?.textContent).toContain('ValueError: boom');
  expect(records[0]?.querySelector('details')).toBeNull();
  expect(records[2]?.textContent).toBe('not a record');

  await act(async () => root.unmount());
  container.remove();
});

test('picking a raw stream follows it instead, with its stderr marked', async () => {
  const bridge = new FakeHostsBridge();
  const {container, root, send} = await mountSheet(bridge);
  await send({type: 'open', target: TARGET});

  await send({type: 'select-log', streamId: '000001'});

  expect(bridge.reads.slice(2)).toEqual([
    {host: 'local', stream: '000001', tail: true},
    {host: 'local', stream: '000001', tail: false},
  ]);
  // The narration's read in flight was withdrawn by its token when the view left it.
  expect(bridge.following).toHaveLength(2);
  expect(new Set(bridge.following).size).toBe(2);
  expect(bridge.withdrawals).toEqual(bridge.following.slice(0, 1));
  const raw = container.ownerDocument.querySelector('[data-testid="log-raw"]');
  expect(raw?.textContent).toBe('step 1\nno space left\n');
  expect([...(raw?.querySelectorAll('mark.stderr') ?? [])].map(mark => mark.textContent)).toEqual([
    'no space left',
  ]);
  expect(raw?.querySelector('mark')?.textContent).not.toContain('step 1');

  await act(async () => root.unmount());
  container.remove();
});

test('the sheet asks for no server log, and closing it lets the logs go', async () => {
  const bridge = new FakeHostsBridge();
  const {container, root, send} = await mountSheet(bridge);
  await send({type: 'open', target: TARGET});
  await send({type: 'refresh'});

  // Host, instance, inspection token: never an include-log flag.
  expect(bridge.inspections).toEqual([
    ['local', INSTANCE, 1],
    ['local', INSTANCE, 2],
  ]);
  expect(container.ownerDocument.body.textContent).not.toContain('Server log tail');
  expect(container.ownerDocument.body.textContent).not.toContain('Load diagnostics');

  const reads = bridge.reads.length;
  await send({type: 'close'});
  expect(bridge.releases).toBe(1);
  expect(container.ownerDocument.querySelector('[data-testid="inspection-logs"]')).toBeNull();
  expect(bridge.reads).toHaveLength(reads);
  expect(bridge.withdrawals).toEqual(bridge.following);

  await act(async () => root.unmount());
  container.remove();
});
