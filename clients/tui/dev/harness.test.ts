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
import {type JsonObject, toJson} from '@bufbuild/protobuf';
import {
  buildRequest,
  decodeResponse,
  decodeServerMessage,
  type EventBatchMessage,
  EventType,
  encodeRequest,
  type ProtocolResponse,
  type RequestBody,
  type RunEvent,
  RunEventSchema,
} from '@vibesys/backend-client';
import {initialCoreState, reduceEventBatch, reduceSnapshot} from '@vibesys/core-state';
import {canonicalJournalEvents, readJournalRecords} from './journal.js';

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
      return;
    } catch (error) {
      const code = (error as {code?: unknown}).code;
      if ((code !== 'EBUSY' && code !== 'ENOTEMPTY') || Date.now() >= deadline) throw error;
      await delay(25);
    }
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

/** Resolves with the first line the server writes that `match` accepts, as raw JSON. */
function nextLine(
  socket: Socket,
  match: (message: Record<string, unknown>) => boolean,
): Promise<string> {
  return new Promise(resolve => {
    let buffer = '';
    socket.setEncoding('utf8');
    socket.on('data', chunk => {
      buffer += chunk;
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const line of lines) {
        if (!line) continue;
        if (match(JSON.parse(line) as Record<string, unknown>)) resolve(line);
      }
    });
  });
}

/** Resolves with the reply to `requestId`, decoded with the client's own codec. */
async function nextResponse(socket: Socket, requestId: string): Promise<ProtocolResponse> {
  return decodeResponse(await nextLine(socket, message => message['request_id'] === requestId));
}

function request(socket: Socket, requestId: string, body: RequestBody): void {
  socket.write(`${encodeRequest(buildRequest(body, requestId))}\n`);
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
async function bootstrapBatch(socketPath: string): Promise<EventBatchMessage> {
  const stream = await connect(socketPath);
  const line = nextLine(stream, message => 'event_batch' in message);
  request(stream, 'sub-1', {case: 'subscribe', value: {}});
  const message = decodeServerMessage(await line);
  if (message.body.case !== 'eventBatch') throw new Error('expected an event batch');
  return message.body.value;
}

function countByType(events: readonly RunEvent[], type: EventType): number {
  return events.filter(event => event.type === type).length;
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
    const snapshotResponse = nextResponse(control, 'snap-1');
    request(control, 'snap-1', {case: 'snapshot', value: {}});
    const snapshot = (await snapshotResponse).snapshot;
    if (snapshot === undefined) throw new Error('expected a snapshot');

    const throughSequence = batch.throughSequence;
    // The bootstrap counts events, not sequences, and the two stopped agreeing
    // when the replay started applying the server's legacy translation: this
    // capture records both spellings of each lifecycle boundary, and the
    // superseded legacy one is dropped before it reaches the wire.
    expect(batch.events).toHaveLength(MID_EXECUTION_BOOTSTRAP);
    // The equal-sequence case is the one `reduceSnapshot` accepts.
    expect(snapshot.sequence).toBe(throughSequence);

    const folded = reduceEventBatch(
      initialCoreState(),
      batch.events,
      batch.activeExecutions,
      throughSequence,
      batch.historyAfterSequence,
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
    const events = batch.events;

    // The capture records `invocation_started` and carries the execution
    // identity under `invocation_id` alone, so both of these are the
    // translation's doing and neither held before it.
    expect(countByType(events, EventType.INVOCATION_STARTED)).toBe(0);
    expect(countByType(events, EventType.AGENT_EXECUTION_STARTED)).toBe(1);

    // Folded with no checkpoint, so what this proves is that the delivered
    // events open the execution, not that the mock also described one.
    const folded = reduceEventBatch(
      initialCoreState(),
      events,
      undefined,
      batch.throughSequence,
      batch.historyAfterSequence,
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
      batch.activeExecutions,
      batch.throughSequence,
      batch.historyAfterSequence,
    );
    expect(reconciled.activeExecutions).toEqual(folded.activeExecutions);

    // The whole capture, over the backfill query, which reads the same replay.
    const control = await connect(socketPath);
    const answer = nextResponse(control, 'events-1');
    request(control, 'events-1', {case: 'events', value: {afterSequence: 0}});
    const all = (await answer).events;
    expect(countByType(all, EventType.AGENT_EXECUTION_STARTED)).toBe(3);
    expect(countByType(all, EventType.AGENT_EXECUTION_FINISHED)).toBe(3);
    expect(countByType(all, EventType.INVOCATION_STARTED)).toBe(0);
    expect(countByType(all, EventType.INVOCATION_FINISHED)).toBe(0);
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
  const golden = JSON.parse(readFileSync(CANONICAL_GOLDEN, 'utf8')) as Record<string, JsonObject[]>;
  expect(Object.keys(golden)).not.toHaveLength(0);
  for (const [fixture, expected] of Object.entries(golden)) {
    // The version 2 canonical JSON of every event a client receives, in order,
    // with proto field names, as `codec.to_dict` writes it.
    const projection = canonicalJournalEvents(readJournalRecords(join(FIXTURE_DIR, fixture))).map(
      event => toJson(RunEventSchema, event, {useProtoFieldName: true}),
    );
    expect(projection).toEqual(expected);
  }
});
