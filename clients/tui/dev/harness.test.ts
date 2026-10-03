/**
 * Process-level tests for the replay harness.
 *
 * The harness is two processes and a shell script that wires them together, so
 * the things that go wrong with it are process lifetime and wire bytes, neither
 * of which a unit test over a helper would see. The tests here therefore run
 * the real scripts and speak the real protocol over a real socket. The parity
 * test at the end is the exception, and says why.
 *
 * `tests/server/test_tui_dev_harness.py` covers the other half, the static
 * response bodies and the recorded fixtures, against the Python models. It
 * cannot reach anything the mock computes at runtime, because CI's pytest job
 * has no JavaScript runtime, and nothing here can call the Python. The
 * canonicalization the two halves share therefore meets in a file: that test
 * writes `canonical-events.golden.json` from the real read path, and the parity
 * test below holds `journal.ts` to it.
 */

import {afterEach, expect, test} from 'bun:test';
import {type ChildProcess, spawn, spawnSync} from 'node:child_process';
import {chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {createConnection, type Socket} from 'node:net';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import type {RunEvent, RunSnapshot} from '@vibesys/backend-client';
import {
  type ActiveExecutionCheckpoint,
  initialCoreState,
  reduceEventBatch,
  reduceSnapshot,
} from '@vibesys/core-state';
import {canonicalJournalEvents, type RunEventRecord, readJournalRecords} from './journal.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const MOCK_UI = join(HERE, 'mock-ui.sh');
const MOCK_SERVER = join(HERE, 'mock-server.ts');
const FIXTURE_DIR = join(HERE, 'fixtures');
const CANONICAL_GOLDEN = join(HERE, 'canonical-events.golden.json');

/** Generous: these wait on process exits, not on anything that should be slow. */
const TEST_TIMEOUT_MS = 30_000;
const WAIT_TIMEOUT_MS = 15_000;

/**
 * Bootstrap size that stops inside the first agent execution.
 *
 * `queue-rs-payloads` opens one at sequence 37 and closes it at 126, and
 * `bad-cpp-round1` opens one at 10 and closes it at 79, so a 40-event bootstrap
 * is delivered mid-execution on either: the client folds an active execution
 * out of it, and a snapshot taken at the same sequence has to agree.
 */
const MID_EXECUTION_BOOTSTRAP = 40;

const cleanups: (() => void | Promise<void>)[] = [];

afterEach(async () => {
  while (cleanups.length > 0) await cleanups.pop()?.();
});

function scratchDirectory(prefix: string): string {
  const directory = mkdtempSync(join(tmpdir(), prefix));
  cleanups.push(() => removeScratchDirectory(directory));
  return directory;
}

/**
 * Removes a scratch directory, retrying while the filesystem still counts a
 * dying process's open files against it. Unlinking an open file succeeds on a
 * local filesystem, but an NFS client silly-renames it to `.nfsXXXX` instead,
 * so until the holder has exited and the client has reaped the rename, the
 * rename refuses to unlink (EBUSY) and the directory stays non-empty
 * (ENOTEMPTY). Both clear on their own once the process is gone; anything else
 * is a real bug and is rethrown immediately.
 */
async function removeScratchDirectory(directory: string): Promise<void> {
  const deadline = Date.now() + WAIT_TIMEOUT_MS;
  while (true) {
    try {
      rmSync(directory, {recursive: true, force: true});
    } catch (error) {
      const code = (error as {code?: unknown}).code;
      if ((code !== 'EBUSY' && code !== 'ENOTEMPTY') || Date.now() >= deadline) throw error;
    }
    // Returning without throwing does not prove the directory is gone: on NFS
    // rmSync can succeed and still leave the emptied directory behind. Success
    // is the directory being absent.
    if (!existsSync(directory)) return;
    if (Date.now() >= deadline) throw new Error(`could not remove ${directory}`);
    await delay(25);
  }
}

/**
 * SIGKILLs `child` and resolves once the process has actually exited. The kill
 * alone is not enough for teardown: signal delivery is asynchronous, so at the
 * moment `kill` returns the process can still hold its files in the scratch
 * directory open, which is exactly what the removal that follows in LIFO order
 * must not race.
 */
function killAndAwaitExit(child: ChildProcess): Promise<void> {
  return new Promise(resolve => {
    if (child.pid === undefined || child.exitCode !== null || child.signalCode !== null) {
      resolve();
      return;
    }
    child.once('exit', () => resolve());
    child.kill('SIGKILL');
  });
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function waitFor(what: string, condition: () => Promise<boolean>): Promise<void> {
  const deadline = Date.now() + WAIT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await condition()) return;
    await delay(25);
  }
  throw new Error(`timed out waiting for ${what}`);
}

/** Whether anything is still accepting connections on `socketPath`. */
function isListening(socketPath: string): Promise<boolean> {
  return new Promise(resolve => {
    const probe = createConnection(socketPath);
    probe.on('connect', () => {
      probe.destroy();
      resolve(true);
    });
    probe.on('error', () => {
      probe.destroy();
      resolve(false);
    });
  });
}

function connect(socketPath: string): Promise<Socket> {
  return new Promise((resolve, reject) => {
    const socket = createConnection(socketPath);
    socket.on('connect', () => {
      cleanups.push(() => void socket.destroy());
      resolve(socket);
    });
    socket.on('error', reject);
  });
}

/** Resolves with the first message the server writes that `match` accepts. */
function nextMessage(
  socket: Socket,
  match: (message: Record<string, unknown>) => boolean,
): Promise<Record<string, unknown>> {
  return new Promise(resolve => {
    let buffer = '';
    socket.setEncoding('utf8');
    socket.on('data', chunk => {
      buffer += chunk;
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const line of lines) {
        if (!line) continue;
        const message = JSON.parse(line) as Record<string, unknown>;
        if (match(message)) resolve(message);
      }
    });
  });
}

function request(socket: Socket, payload: Record<string, unknown>): void {
  socket.write(`${JSON.stringify({protocol_version: 1, ...payload})}\n`);
}

/** Starts a mock server bound to `socketPath` and waits for it to accept. */
async function startMockServer(socketPath: string, args: string[]): Promise<void> {
  const server = spawn(
    'bun',
    [MOCK_SERVER, '--socket', socketPath, '--owner-pid', String(process.pid), ...args],
    {stdio: 'ignore'},
  );
  cleanups.push(() => killAndAwaitExit(server));
  await waitFor('the mock server to bind', () => isListening(socketPath));
}

/** Subscribes on its own connection and resolves with the bootstrap batch. */
async function bootstrapBatch(socketPath: string): Promise<Record<string, unknown>> {
  const stream = await connect(socketPath);
  const batch = nextMessage(stream, message => message['type'] === 'event_batch');
  request(stream, {request_id: 'sub-1', type: 'subscribe'});
  return batch;
}

function countByType(events: readonly RunEvent[], type: string): number {
  return events.filter(event => event.type === type).length;
}

/**
 * The run-level spine a tail-bounded bootstrap prepends, duplicated from
 * `_BOOTSTRAP_SPINE_TYPES` in `src/server/journal.py` rather than imported
 * from `mock-server.ts`, which runs the server's `main` on import.
 */
const SPINE_TYPES = new Set([
  'run_started',
  'run_status_changed',
  'run_finished',
  'run_failed',
  'run_interrupted',
  'configuration_failed',
  'round_finished',
  'experiments_changed',
  'chat_thread_created',
]);

/** The canonical stream the mock serves for `fixture`, computed the same way. */
function canonicalFixture(fixture: string): RunEventRecord[] {
  return canonicalJournalEvents(readJournalRecords(join(FIXTURE_DIR, fixture)));
}

/** What a tail-bounded bootstrap owes: the spine at or below `floor`, then the rest. */
function spineBootstrap(events: RunEventRecord[], floor: number): RunEventRecord[] {
  return [
    ...events.filter(event => (event.sequence ?? 0) <= floor && SPINE_TYPES.has(event.type ?? '')),
    ...events.filter(event => (event.sequence ?? 0) > floor),
  ];
}

interface Collected {
  /** Raw lines, for byte-level comparisons. */
  lines: string[];
  messages: Record<string, unknown>[];
}

/**
 * Collects every line the server writes on `socket`, keeping the raw bytes
 * alongside the parse. Attached before the request goes out, unlike a late
 * `nextMessage`, so a multi-message exchange cannot lose its earlier lines.
 */
function collectMessages(socket: Socket): Collected {
  const collected: Collected = {lines: [], messages: []};
  let buffer = '';
  socket.setEncoding('utf8');
  socket.on('data', chunk => {
    buffer += chunk;
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line) continue;
      collected.lines.push(line);
      collected.messages.push(JSON.parse(line) as Record<string, unknown>);
    }
  });
  return collected;
}

function eventBatches(collected: Collected): Record<string, unknown>[] {
  return collected.messages.filter(message => message['type'] === 'event_batch');
}

/**
 * Subscribes on a fresh connection with `fields` merged into the request and
 * resolves once the bootstrap batch has arrived.
 */
async function subscribeCollecting(
  socketPath: string,
  fields: Record<string, unknown>,
): Promise<{socket: Socket; collected: Collected}> {
  const socket = await connect(socketPath);
  const collected = collectMessages(socket);
  request(socket, {request_id: 'sub-1', type: 'subscribe', ...fields});
  await waitFor('the bootstrap batch', async () => eventBatches(collected).length > 0);
  return {socket, collected};
}

test(
  'terminates the mock server when the client exits before it subscribes',
  async () => {
    const directory = scratchDirectory('vs-mock-owner-');
    const socketPath = join(directory, 'mock.sock');
    const logPath = join(directory, 'mock.log');
    const clientPath = join(directory, 'client.sh');
    // Stands in for a TUI that never reaches its first `subscribe`: a missing
    // or incompatible runtime, or an exception during initialisation.
    writeFileSync(clientPath, '#!/usr/bin/env bash\nexit 3\n');
    chmodSync(clientPath, 0o755);
    // Best effort, so a regression leaves no server behind for the next test.
    cleanups.push(() => void spawnSync('pkill', ['-f', socketPath]));

    const exitCode = await new Promise<number | null>(resolve => {
      const script = spawn(MOCK_UI, ['--speed', '0'], {
        env: {
          ...process.env,
          VS_MOCK_SOCKET: socketPath,
          VS_MOCK_LOG: logPath,
          VS_MOCK_CLIENT: clientPath,
        },
        stdio: 'ignore',
      });
      script.on('close', code => resolve(code));
    });

    // The runner waits for the bind before it execs the client and reports 1 if
    // that never happened, so 3 is also the evidence that this was a failure
    // after the socket existed.
    expect(exitCode).toBe(3);

    await waitFor('the mock server to exit', async () => !(await isListening(socketPath)));
    expect(existsSync(socketPath)).toBe(false);
  },
  TEST_TIMEOUT_MS,
);

test(
  'keeps the active execution when an equal-sequence snapshot follows the bootstrap',
  async () => {
    const directory = scratchDirectory('vs-mock-snapshot-');
    const socketPath = join(directory, 'mock.sock');
    await startMockServer(socketPath, [
      '--fixture',
      'queue-rs-payloads.jsonl',
      '--bootstrap',
      String(MID_EXECUTION_BOOTSTRAP),
      '--paused',
    ]);
    const batch = await bootstrapBatch(socketPath);

    // A second connection, the way the TUI queries while its stream runs. Boot
    // issues both concurrently, so this answer can land after the batch.
    const control = await connect(socketPath);
    const snapshotMessage = nextMessage(control, message => message['request_id'] === 'snap-1');
    request(control, {request_id: 'snap-1', type: 'query.snapshot'});
    const snapshot = (await snapshotMessage)['snapshot'] as RunSnapshot;

    const throughSequence = batch['through_sequence'] as number;
    // The bootstrap counts events, not sequences, and the two stopped agreeing
    // when the replay started applying the server's legacy translation: this
    // capture records both spellings of each lifecycle boundary, and the
    // superseded legacy one is dropped before it reaches the wire.
    expect(batch['events']).toHaveLength(MID_EXECUTION_BOOTSTRAP);
    // The equal-sequence case is the one `reduceSnapshot` accepts.
    expect(snapshot.sequence).toBe(throughSequence);

    const folded = reduceEventBatch(
      initialCoreState(),
      batch['events'] as RunEvent[],
      batch['active_executions'] as ActiveExecutionCheckpoint | undefined,
      throughSequence,
      batch['history_after_sequence'] as number,
    );
    // Precondition: the bootstrap really does stop inside an invocation.
    const running = Object.keys(folded.activeExecutions);
    expect(running).toHaveLength(1);

    expect(Object.keys(reduceSnapshot(folded, snapshot).activeExecutions)).toEqual(running);
  },
  TEST_TIMEOUT_MS,
);

test(
  'replays a legacy capture with the executions the read path translates into it',
  async () => {
    const directory = scratchDirectory('vs-mock-legacy-');
    const socketPath = join(directory, 'mock.sock');
    await startMockServer(socketPath, [
      '--fixture',
      'bad-cpp-round1.jsonl',
      '--bootstrap',
      String(MID_EXECUTION_BOOTSTRAP),
      '--paused',
    ]);
    const batch = await bootstrapBatch(socketPath);
    const events = batch['events'] as RunEvent[];

    // The capture records `invocation_started` and carries the execution
    // identity under `invocation_id` alone, so both of these are the
    // translation's doing and neither held before it.
    expect(countByType(events, 'invocation_started')).toBe(0);
    expect(countByType(events, 'agent_execution_started')).toBe(1);

    // Folded with no checkpoint, so what this proves is that the delivered
    // events open the execution, not that the mock also described one.
    const folded = reduceEventBatch(
      initialCoreState(),
      events,
      undefined,
      batch['through_sequence'] as number,
      batch['history_after_sequence'] as number,
    );
    const executions = Object.values(folded.activeExecutions);
    expect(executions).toHaveLength(1);
    // Synthesized by the translation, which is the only reason the pane has an
    // activity to render: the legacy payload carries none.
    expect(executions[0]?.stage).toBe('orchestrator');
    expect(executions[0]?.activity).toEqual({mode: 'thinking', summary: 'Planning', tool: null});
    // The checkpoint the same batch carries describes that same execution.
    const reconciled = reduceEventBatch(
      initialCoreState(),
      events,
      batch['active_executions'] as ActiveExecutionCheckpoint,
      batch['through_sequence'] as number,
      batch['history_after_sequence'] as number,
    );
    expect(reconciled.activeExecutions).toEqual(folded.activeExecutions);

    // The whole capture, over the backfill query, which reads the same replay.
    const control = await connect(socketPath);
    const answer = nextMessage(control, message => message['request_id'] === 'events-1');
    request(control, {request_id: 'events-1', type: 'query.events', after_sequence: 0});
    const all = (await answer)['events'] as RunEvent[];
    expect(countByType(all, 'agent_execution_started')).toBe(3);
    expect(countByType(all, 'agent_execution_finished')).toBe(3);
    expect(countByType(all, 'invocation_started')).toBe(0);
    expect(countByType(all, 'invocation_finished')).toBe(0);
  },
  TEST_TIMEOUT_MS,
);

test(
  'resumes a reconnect from after_sequence instead of replaying the bootstrap',
  async () => {
    const directory = scratchDirectory('vs-mock-resume-');
    const socketPath = join(directory, 'mock.sock');
    await startMockServer(socketPath, [
      '--fixture',
      'queue-rs-payloads.jsonl',
      '--bootstrap',
      String(MID_EXECUTION_BOOTSTRAP),
      '--paused',
    ]);
    const delivered = canonicalFixture('queue-rs-payloads.jsonl').slice(0, MID_EXECUTION_BOOTSTRAP);
    const through = delivered.at(-1)?.sequence ?? 0;

    const boot = await subscribeCollecting(socketPath, {});
    const bootstrap = eventBatches(boot.collected)[0];
    expect(bootstrap?.['events']).toEqual(delivered);
    const store = bootstrap?.['store_id'] as string;
    // The stream drops, as on a suspend or a crashed client.
    boot.socket.destroy();

    // The client redials with its fold's cursor and the store that numbered it
    // (`PersistentEventStream`), here from mid-bootstrap: the canonical stream
    // has sequence gaps, so what is owed is events after the cursor, not a
    // count of them.
    const cursor = delivered[24]?.sequence ?? 0;
    const resumed = await subscribeCollecting(socketPath, {
      after_sequence: cursor,
      store_id: store,
    });
    const subscribed = resumed.collected.messages.find(message => message['type'] === 'subscribed');
    expect(subscribed?.['latest_sequence']).toBe(through);
    const backfill = eventBatches(resumed.collected)[0];
    expect(backfill?.['events']).toEqual(delivered.slice(25));
    expect(backfill?.['through_sequence']).toBe(through);
    expect(backfill?.['history_after_sequence']).toBe(0);
    resumed.socket.destroy();

    // A cursor already at the head owes nothing: an empty batch, not a replay.
    const caughtUp = await subscribeCollecting(socketPath, {
      after_sequence: through,
      store_id: store,
    });
    const empty = eventBatches(caughtUp.collected)[0];
    expect(empty?.['events']).toEqual([]);
    expect(empty?.['through_sequence']).toBe(through);
  },
  TEST_TIMEOUT_MS,
);

test(
  'rebootstraps a resume whose store id does not match the live store',
  async () => {
    const directory = scratchDirectory('vs-mock-storeid-');
    const socketPath = join(directory, 'mock.sock');
    await startMockServer(socketPath, [
      '--fixture',
      'queue-rs-payloads.jsonl',
      '--bootstrap',
      String(MID_EXECUTION_BOOTSTRAP),
      '--paused',
    ]);
    const delivered = canonicalFixture('queue-rs-payloads.jsonl').slice(0, MID_EXECUTION_BOOTSTRAP);
    const through = delivered.at(-1)?.sequence ?? 0;

    // A caught-up cursor, but into some other store: it numbers a log this
    // server is not serving, so honoring it would silently skip the whole run.
    // `subscription_bootstrap` drops the cursor and replays from zero.
    const {collected} = await subscribeCollecting(socketPath, {
      after_sequence: through,
      store_id: 'a-store-this-server-never-served',
    });
    const batch = eventBatches(collected)[0];
    expect(batch?.['events']).toEqual(delivered);
    expect(batch?.['through_sequence']).toBe(through);
    // The batch names the live store, so the client re-keys its fold to it.
    expect(batch?.['store_id']).toBe(`mock-store-${delivered[0]?.run_id ?? ''}`);
  },
  TEST_TIMEOUT_MS,
);

test(
  'bounds a tail subscription and prepends the run spine below its floor',
  async () => {
    const directory = scratchDirectory('vs-mock-tail-');
    const socketPath = join(directory, 'mock.sock');
    await startMockServer(socketPath, [
      '--fixture',
      'queue-rs-payloads.jsonl',
      '--bootstrap',
      String(MID_EXECUTION_BOOTSTRAP),
      '--paused',
    ]);
    const delivered = canonicalFixture('queue-rs-payloads.jsonl').slice(0, MID_EXECUTION_BOOTSTRAP);
    const latest = delivered.at(-1)?.sequence ?? 0;
    const tail = 10;
    const floor = latest - tail;

    const {collected} = await subscribeCollecting(socketPath, {tail});
    const batch = eventBatches(collected)[0];
    const expected = spineBootstrap(delivered, floor);
    // Preconditions on the fixture: the spine below the floor is non-empty and
    // most history is elided, so this exercises the bound, not a full replay.
    const spine = delivered.filter(
      event => (event.sequence ?? 0) <= floor && SPINE_TYPES.has(event.type ?? ''),
    );
    expect(spine).not.toHaveLength(0);
    expect(expected.length).toBeLessThan(delivered.length);
    expect(batch?.['events']).toEqual(expected);
    // The declared floor is what tells the TUI to backfill below it.
    expect(batch?.['history_after_sequence']).toBe(floor);
    expect(batch?.['through_sequence']).toBe(latest);
  },
  TEST_TIMEOUT_MS,
);

test(
  'delivers a resumed burst as one multi-event batch and rebootstraps an outrun tail',
  async () => {
    const directory = scratchDirectory('vs-mock-burst-');
    const socketPath = join(directory, 'mock.sock');
    await startMockServer(socketPath, [
      '--fixture',
      'queue-rs-payloads.jsonl',
      '--bootstrap',
      String(MID_EXECUTION_BOOTSTRAP),
      '--paused',
      '--speed',
      '0',
    ]);
    const canonical = canonicalFixture('queue-rs-payloads.jsonl');
    const final = canonical.at(-1)?.sequence ?? 0;
    const tail = 10;

    const unbounded = await subscribeCollecting(socketPath, {});
    const bounded = await subscribeCollecting(socketPath, {tail});
    const control = await connect(socketPath);
    const resumed = nextMessage(control, message => message['request_id'] === 'resume-1');
    request(control, {request_id: 'resume-1', type: 'command.resume'});
    await resumed;
    await waitFor(
      'both live batches',
      async () =>
        eventBatches(unbounded.collected).length >= 2 &&
        eventBatches(bounded.collected).length >= 2,
    );

    // At --speed 0 the whole remainder is due at one wake, so it reaches the
    // unbounded subscription as a single batch, the way `_stream` sends
    // everything since its last `wait_for_change` in one `EventBatchMessage`.
    const live = eventBatches(unbounded.collected)[1];
    const remainder = canonical.slice(MID_EXECUTION_BOOTSTRAP);
    expect(remainder.length).toBeGreaterThan(1);
    expect(live?.['events']).toEqual(remainder);
    expect(live?.['through_sequence']).toBe(final);
    expect(live?.['history_after_sequence']).toBe(0);

    // The tail subscription cannot take that batch: more landed in one wake
    // than its bound was willing to replay, so it is bootstrapped again at a
    // fresh floor rather than sent the window the bound was meant to exclude.
    const floor = final - tail;
    const reboot = eventBatches(bounded.collected)[1];
    expect(reboot?.['history_after_sequence']).toBe(floor);
    expect(reboot?.['events']).toEqual(spineBootstrap(canonical, floor));
    expect(reboot?.['through_sequence']).toBe(final);
  },
  TEST_TIMEOUT_MS,
);

test(
  'replays byte-identical envelopes across two runs of the same fixture',
  async () => {
    const canonical = canonicalFixture('queue-rs-payloads.jsonl');
    const final = canonical.at(-1)?.sequence ?? 0;
    const transcript = async (label: string): Promise<string[]> => {
      const directory = scratchDirectory(`vs-mock-replay-${label}-`);
      const socketPath = join(directory, 'mock.sock');
      await startMockServer(socketPath, [
        '--fixture',
        'queue-rs-payloads.jsonl',
        '--bootstrap',
        String(MID_EXECUTION_BOOTSTRAP),
        '--speed',
        '0',
      ]);
      const stream = await subscribeCollecting(socketPath, {});
      await waitFor('the replay to finish', async () =>
        eventBatches(stream.collected).some(batch => batch['through_sequence'] === final),
      );
      // Control answers after the stream is done, so the replay clock reads
      // the same instant in both runs.
      const control = await connect(socketPath);
      const answers = collectMessages(control);
      request(control, {request_id: 'snap-1', type: 'query.snapshot'});
      request(control, {request_id: 'perf-1', type: 'query.performance'});
      await waitFor('both control answers', async () => answers.messages.length >= 2);
      return [...stream.collected.lines, ...answers.lines];
    };

    const first = await transcript('a');
    const second = await transcript('b');
    // Full envelopes, not just event payloads: subscribed, both batches, and
    // the control responses, byte for byte.
    expect(second).toEqual(first);
    // The envelope clock is the recording's, not the wall's: after the replay
    // has finished, responses are stamped with the final event's timestamp.
    const snapshot = JSON.parse(first.find(line => line.includes('"snap-1"')) ?? '{}') as Record<
      string,
      unknown
    >;
    expect(snapshot['timestamp']).toBe(canonical.at(-1)?.timestamp);
  },
  TEST_TIMEOUT_MS,
);

test(
  'kill cleanup resolves only after the mock server has exited',
  async () => {
    const directory = scratchDirectory('vs-mock-exit-');
    const socketPath = join(directory, 'mock.sock');
    const server = spawn(
      'bun',
      [MOCK_SERVER, '--socket', socketPath, '--owner-pid', String(process.pid), '--paused'],
      {stdio: 'ignore'},
    );
    cleanups.push(() => killAndAwaitExit(server));
    await waitFor('the mock server to bind', () => isListening(socketPath));

    await killAndAwaitExit(server);
    // What LIFO teardown relies on: by the time the kill cleanup resolves, the
    // process is gone, so the removal that follows cannot race its open files.
    expect(server.exitCode !== null || server.signalCode !== null).toBe(true);
    // And it is idempotent, because afterEach runs the pushed cleanup again.
    await killAndAwaitExit(server);
  },
  TEST_TIMEOUT_MS,
);

test(
  'scratch removal outwaits a straggler that still holds a file open',
  async () => {
    const directory = scratchDirectory('vs-mock-straggler-');
    const heldPath = join(directory, 'held.log');
    const holder = spawn('bash', ['-c', 'exec 3>"$1"; sleep 0.4', 'bash', heldPath], {
      stdio: 'ignore',
    });
    cleanups.push(() => killAndAwaitExit(holder));
    await waitFor('the straggler to open its file', async () => existsSync(heldPath));

    // On a silly-renaming tmpdir this is the exact shape the suite used to die
    // on: the open file cannot be unlinked until the holder exits, so the first
    // attempts fail and the removal has to retry. On a local filesystem the
    // first attempt simply succeeds; either way the directory must end up gone.
    await removeScratchDirectory(directory);
    expect(existsSync(directory)).toBe(false);
  },
  TEST_TIMEOUT_MS,
);

/**
 * The one test here that runs no process: what it checks is a hand port of
 * Python, and the golden it reads is the only place the two languages meet.
 */
test('canonicalizes recorded journals the way the backend read path does', () => {
  const golden = JSON.parse(readFileSync(CANONICAL_GOLDEN, 'utf8')) as Record<string, unknown[][]>;
  expect(Object.keys(golden)).not.toHaveLength(0);
  for (const [fixture, expected] of Object.entries(golden)) {
    const records = readJournalRecords(join(FIXTURE_DIR, fixture));
    const recordedType = new Map(records.map(record => [record.sequence, record.type]));
    // The same projection `_canonical_projection` writes: identity and order for
    // every delivered event, and the whole payload for the ones the translation
    // rebuilt, which are exactly those whose type no longer matches the record.
    const projection = canonicalJournalEvents(records).map(event => {
      const entry: unknown[] = [event.sequence, event.type, event.execution_id ?? null];
      if (event.type !== recordedType.get(event.sequence)) entry.push(event.data ?? null);
      return entry;
    });
    expect(projection).toEqual(expected);
  }
});
