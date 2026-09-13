/**
 * Replays a recorded `run-events.jsonl` over the control protocol so the real,
 * unmodified TUI can be driven without a backend, an agent, or any tokens.
 *
 * This is development-only tooling. It lives outside `src/`, so it is neither
 * compiled into `dist` nor included in the published package; nothing on the
 * shipping path imports it. The TUI reaches it exactly the way it reaches the
 * Python server, by connecting to `VIBESYS_CONTROL_SOCKET`:
 *
 *   bun clients/tui/dev/mock-server.ts --socket /tmp/vs-mock.sock &
 *   VIBESYS_CONTROL_SOCKET=/tmp/vs-mock.sock bun clients/tui/dist/index.js
 *
 * `mock-ui.sh` wraps both halves.
 *
 * The protocol is newline-delimited JSON in both directions with no handshake:
 * the client says nothing on connect and correlates purely by `request_id`.
 * Every response must carry `protocol_version: 1`, `request_id`, and `ok`, or
 * the client destroys the socket. The TUI opens three connections to this one
 * path (control, event stream, and one per chat question), so connections are
 * handled independently and none of them is closed early.
 *
 * The response bodies live in `mock-responses.json` rather than in this file so
 * `tests/server/test_tui_dev_harness.py` can validate the exact bytes that go
 * on the wire against the Python protocol contract.
 *
 * What goes on the wire is the recorded journal as the server's read path would
 * have served it, not as it was recorded: `journal.ts` applies the same legacy
 * translation before anything is replayed.
 */

import {readFileSync, unlinkSync} from 'node:fs';
import {createServer, type Socket} from 'node:net';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import type {ActiveExecutionCheckpoint} from '@vibesys/core-state';
import {
  canonicalJournalEvents,
  optionalString,
  type RunEventRecord,
  readJournalRecords,
  stringOr,
} from './journal.js';

/**
 * One entry of the liveness checkpoint the real server puts on a snapshot and
 * on every event batch. Taken from the generated protocol types, so a field
 * added to `ActiveAgentExecution` is a typecheck error here rather than a
 * silently thinner checkpoint on the wire.
 */
type ExecutionCheckpoint = ActiveExecutionCheckpoint[number];

interface Options {
  socketPath: string;
  fixture: string;
  /** Wall-clock multiplier. 0 replays every event at once. */
  speed: number;
  /** Longest gap honored between two events, so idle agent turns do not stall. */
  maxGapMs: number;
  /** Holds the replay before the first live event until `/resume` in the TUI. */
  startPaused: boolean;
  /** Events delivered instantly at boot, as the recorded history. */
  bootstrap: number;
  /** Process this server exists to feed. `null` when it was started on its own. */
  ownerPid: number | null;
  verbose: boolean;
}

/**
 * How long to wait after the last subscriber leaves before exiting. Long enough
 * to survive a reconnect, short enough that a killed session leaves nothing.
 */
const DISCONNECT_GRACE_MS = 1_500;

/** How often the owner watchdog checks that the client process is still there. */
const OWNER_POLL_MS = 250;

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE_DIR = join(HERE, 'fixtures');
const DEFAULT_FIXTURE = join(FIXTURE_DIR, 'bad-cpp-round1.jsonl');

/**
 * Every static response body this server can send, keyed by request type.
 *
 * Read from disk rather than written inline so the Python protocol test can
 * check the same bytes: a body inlined here would be checkable only by copying
 * it into that test, where the copy could silently stop matching.
 */
const RESPONSE_BODIES = JSON.parse(
  readFileSync(join(HERE, 'mock-responses.json'), 'utf8'),
) as Record<string, Record<string, unknown>>;

function responseBody(type: string): Record<string, unknown> {
  const body = RESPONSE_BODIES[type];
  if (body === undefined) throw new Error(`mock-responses.json has no body for ${type}`);
  return body;
}

/**
 * `base` with the runtime `values` merged over it.
 *
 * A value whose key the checked-in body does not already carry is an error:
 * the file is what the protocol test validates, so a call site writing a key
 * the file lacks would put something unchecked on the wire, which is exactly
 * how a renamed protocol field would go unnoticed here.
 */
function withValues(base: unknown, values: Record<string, unknown>): Record<string, unknown> {
  if (typeof base !== 'object' || base === null || Array.isArray(base)) {
    throw new Error(`mock-responses.json holds ${JSON.stringify(base)} where an object is needed`);
  }
  const merged = base as Record<string, unknown>;
  for (const key of Object.keys(values)) {
    if (!(key in merged)) throw new Error(`mock-responses.json body has no key ${key}`);
  }
  return {...merged, ...values};
}

function parseOptions(argv: string[]): Options {
  const value = (flag: string): string | undefined => {
    const index = argv.indexOf(flag);
    return index === -1 ? undefined : argv[index + 1];
  };
  const number = (flag: string, fallback: number): number => {
    const raw = value(flag);
    if (raw === undefined) return fallback;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) throw new Error(`${flag} expects a number, got ${raw}`);
    return parsed;
  };
  return {
    socketPath: value('--socket') ?? '/tmp/vs-mock.sock',
    fixture: value('--fixture') ?? DEFAULT_FIXTURE,
    speed: number('--speed', 8),
    maxGapMs: number('--max-gap', 400),
    startPaused: argv.includes('--paused'),
    bootstrap: number('--bootstrap', 0),
    ownerPid: value('--owner-pid') === undefined ? null : number('--owner-pid', 0),
    verbose: argv.includes('--verbose'),
  };
}

/**
 * Reads a fixture by path, by bare name against the bundled fixture directory,
 * with or without a `.gz` suffix. The bundled ones are plain, so they stay
 * greppable and hand-editable; a run you recorded yourself is accepted either
 * way.
 */
function resolveFixture(name: string): string {
  const candidates = name.includes('/')
    ? [name, `${name}.gz`]
    : [
        join(FIXTURE_DIR, name),
        join(FIXTURE_DIR, `${name}.gz`),
        join(FIXTURE_DIR, `${name}.jsonl`),
        join(FIXTURE_DIR, `${name}.jsonl.gz`),
        name,
      ];
  for (const candidate of candidates) {
    try {
      readFileSync(candidate);
      return candidate;
    } catch {
      // Try the next spelling.
    }
  }
  throw new Error(`no fixture found for ${name}`);
}

/**
 * The run's lifecycle status, derived from the events the replay has delivered.
 *
 * `RunEvent.status` is an `EventStatus` (`active`, `answered`, `consumed`), a
 * different closed set from the `RunStatus` this field takes (`starting`,
 * `running`, `pausing`, `paused`, `completed`, `failed`). Echoing the last
 * event's own status therefore answered a snapshot query with a value the
 * protocol does not allow there: the recorded fixtures carry `active` on 235
 * events and `answered` on four. What the client asks for is the run's status,
 * so it comes from the lifecycle events instead.
 *
 * Newest first, because the latest lifecycle event is the current one. A
 * fixture recorded after the lifecycle became a state machine carries
 * `run_status_changed` and states the status outright; one recorded before it
 * has only the coarse start and end events, which map onto the same set.
 */
function replayRunStatus(delivered: RunEventRecord[]): string {
  for (let index = delivered.length - 1; index >= 0; index -= 1) {
    const event = delivered[index];
    if (event === undefined) continue;
    if (event.type === 'run_status_changed') {
      const status = event.data?.['status'];
      if (typeof status === 'string') return status;
    }
    if (event.type === 'run_finished') return 'completed';
    if (event.type === 'run_failed' || event.type === 'run_interrupted') return 'failed';
    if (event.type === 'run_started') return 'running';
  }
  // Nothing delivered yet, so the run has started but reported nothing.
  return 'starting';
}

/** Run-ending event types, the same set the client's `foldEvent` terminates on. */
function isRunTerminal(type: string | undefined): boolean {
  return (
    type === 'run_finished' ||
    type === 'run_failed' ||
    type === 'run_interrupted' ||
    type === 'configuration_failed'
  );
}

/**
 * The agent executions still running after the delivered prefix.
 *
 * Derived rather than stored: the events are the only description of the run
 * the harness has, so a second hand-maintained checkpoint could only drift from
 * them. `mock-responses.json` therefore keeps `active_executions` empty and
 * every answer that carries one, the snapshot and each event batch, takes it
 * from here.
 *
 * The lifecycle rules are the client's own, in `applyAgentExecutionEvent`: a
 * start opens an execution, an activity change updates it, a finish closes it,
 * and a terminal run event closes all of them the way the real server's
 * tracker interrupts what is still running.
 */
function activeExecutionsFrom(delivered: RunEventRecord[]): ActiveExecutionCheckpoint {
  const active = new Map<string, ExecutionCheckpoint>();
  for (const event of delivered) {
    if (isRunTerminal(event.type)) active.clear();
    // `execution_id` only, never the legacy `invocation_id`: the checkpoint has
    // to describe the state the client folded, and the client keys executions
    // by `execution_id` alone. A capture that predates the field still reaches
    // here with one, because `journal.ts` has already applied the same
    // execution-identity translation the server's read path applies.
    const executionId = event.execution_id;
    const data = event.data;
    if (typeof executionId !== 'string' || !data) continue;
    if (data['kind'] === 'agent_execution_started') {
      const agentKind = stringOr(event.agent_kind, 'agent');
      active.set(executionId, {
        execution_id: executionId,
        agent_kind: agentKind,
        round_label: stringOr(event.round_label, ''),
        stage: stringOr(data['stage'], agentKind),
        attempt: typeof data['attempt'] === 'number' ? data['attempt'] : null,
        assignment: stringOr(data['user_prompt'], ''),
        started_at: stringOr(event.timestamp, ''),
        // Carried through, not rebuilt: `test_tui_dev_harness.py` validates
        // every fixture line as a `RunEvent`, so a recorded payload is already
        // an `AgentExecutionActivityData`, closed `mode` set included, and a
        // translated one was built as that type in `journal.ts`.
        activity: data['activity'] as ExecutionCheckpoint['activity'],
        driver: optionalString(data['driver']),
        provider: optionalString(data['provider']),
        model: optionalString(data['model']),
      });
    }
    if (data['kind'] === 'agent_execution_activity_changed') {
      const current = active.get(executionId);
      // The activity event's own payload is the activity, so it replaces the
      // stored one whole rather than being copied field by field.
      if (current !== undefined) {
        active.set(executionId, {...current, activity: data as ExecutionCheckpoint['activity']});
      }
    }
    if (data['kind'] === 'agent_execution_finished') active.delete(executionId);
  }
  return [...active.values()];
}

/**
 * Hypotheses for the experiment log, from `<fixture>.experiments.json` beside
 * the fixture when one exists.
 *
 * Recorded journals hold events, not the projected experiment table, and the
 * header and log render the hypothesis title rather than anything in the event
 * stream. A sidecar keeps that title pinnable without inventing event types the
 * backend never emits.
 */
function loadExperiments(fixturePath: string): unknown[] {
  const sidecar = `${fixturePath.replace(/\.gz$/, '').replace(/\.jsonl$/, '')}.experiments.json`;
  try {
    return JSON.parse(readFileSync(sidecar, 'utf8')) as unknown[];
  } catch {
    return [];
  }
}

/** Milliseconds to wait before `next`, from the recorded timestamps. */
function gapMs(previous: RunEventRecord, next: RunEventRecord, options: Options): number {
  if (options.speed === 0) return 0;
  const from = Date.parse(previous.timestamp ?? '');
  const to = Date.parse(next.timestamp ?? '');
  if (!Number.isFinite(from) || !Number.isFinite(to)) return 0;
  return Math.min(Math.max(to - from, 0) / options.speed, options.maxGapMs);
}

function writeLine(socket: Socket, payload: unknown): void {
  if (socket.destroyed) return;
  socket.write(`${JSON.stringify(payload)}\n`);
}

/** The client's read loop, mirrored: split on newline, keep the partial tail. */
function readLines(socket: Socket, onLine: (line: Record<string, unknown>) => void): void {
  let buffer = '';
  socket.setEncoding('utf8');
  socket.on('data', chunk => {
    buffer += chunk;
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line) continue;
      try {
        onLine(JSON.parse(line) as Record<string, unknown>);
      } catch {
        // A malformed request line is the client's problem; staying up is
        // more useful here than mirroring the real server's strictness.
      }
    }
  });
  socket.on('error', () => undefined);
}

class Replay {
  readonly events: RunEventRecord[];
  readonly #options: Options;
  readonly #subscribers = new Set<Socket>();
  #cursor = 0;
  #started = false;
  #paused: boolean;
  #timer: ReturnType<typeof setTimeout> | null = null;

  constructor(events: RunEventRecord[], options: Options) {
    this.events = events;
    this.#options = options;
    this.#paused = options.startPaused;
  }

  get runId(): string {
    return this.events[0]?.run_id ?? 'mock-run';
  }

  /**
   * The one sequence space this replay ever serves.
   *
   * A real server swaps stores when it attaches a run's durable log, and the
   * client re-folds when the id changes. The mock replays a finished log from
   * the start, so its id is constant and no batch ever asks for a re-fold.
   */
  get storeId(): string {
    return `mock-store-${this.runId}`;
  }

  /**
   * Sequence of the newest event delivered so far, or 0 before any.
   *
   * The cursor is a count, not an index, so a zero cursor means nothing has
   * been sent. Clamping it to index 0 instead reported the first event's
   * sequence before that event had gone anywhere, and the client then treated
   * it as already seen.
   */
  get latestSequence(): number {
    if (this.#cursor === 0) return 0;
    return this.events[this.#cursor - 1]?.sequence ?? 0;
  }

  get delivered(): RunEventRecord[] {
    return this.events.slice(0, this.#cursor);
  }

  /** Liveness checkpoint for everything delivered so far. */
  get activeExecutions(): ActiveExecutionCheckpoint {
    return activeExecutionsFrom(this.delivered);
  }

  /**
   * Backfill range, in current-pass numbering. The stream advertises
   * `history_after_sequence: 0`, so the client should never need this; it is
   * answered correctly rather than left to disagree with the live stream.
   */
  eventsInRange(after: number, before: number | null): RunEventRecord[] {
    return this.events.filter(event => {
      const sequence = event.sequence ?? 0;
      return sequence > after && (before === null || sequence < before);
    });
  }

  /** Called once the last event-stream subscriber has gone. */
  onLastSubscriberGone: (() => void) | null = null;
  /** Called when a subscriber arrives, so a pending exit can be called off. */
  onSubscriberArrived: (() => void) | null = null;

  addSubscriber(socket: Socket): void {
    this.onSubscriberArrived?.();
    this.#subscribers.add(socket);
    socket.on('close', () => {
      this.#subscribers.delete(socket);
      if (this.#subscribers.size === 0) this.onLastSubscriberGone?.();
    });
  }

  broadcast(message: unknown): void {
    for (const socket of this.#subscribers) writeLine(socket, message);
  }

  pause(): void {
    this.#paused = true;
    if (this.#timer !== null) {
      clearTimeout(this.#timer);
      this.#timer = null;
    }
  }

  resume(): void {
    if (!this.#paused) return;
    this.#paused = false;
    this.#step();
  }

  get paused(): boolean {
    return this.#paused;
  }

  /**
   * Emits everything the bootstrap covers, then schedules the rest. Called once
   * the first subscriber arrives so a replay never runs out before anyone sees
   * it.
   */
  /**
   * Advances the cursor over the bootstrap block without emitting it, so a
   * caller can send those events as recorded history in one batch.
   *
   * Separate from `start` because the subscribe handler has to sample
   * `delivered` after this and before anything streams; doing both in `start`
   * meant the bootstrap events were skipped by the cursor and never sent at
   * all.
   */
  primeBootstrap(): void {
    if (this.#cursor > 0) return;
    this.#cursor = Math.min(Math.max(this.#options.bootstrap, 0), this.events.length);
  }

  /** Begins streaming whatever the bootstrap did not already cover. */
  start(): void {
    if (this.#started) return;
    this.#started = true;
    if (!this.#paused) this.#step();
  }

  #step(): void {
    if (this.#paused) return;
    if (this.#cursor >= this.events.length) return;
    const event = this.events[this.#cursor];
    if (event === undefined) return;
    this.#cursor += 1;
    this.broadcast({
      type: 'event_batch',
      events: [event],
      through_sequence: event.sequence,
      active_executions: this.activeExecutions,
      store_id: this.storeId,
    });
    if (this.#options.verbose) {
      process.stderr.write(`mock: seq ${String(event.sequence)} ${String(event.type)}\n`);
    }
    const next = this.events[this.#cursor];
    const delay = next === undefined ? 0 : gapMs(event, next, this.#options);
    this.#timer = setTimeout(() => this.#step(), delay);
    this.#timer.unref?.();
  }
}

/**
 * Whether `pid` still names a live process.
 *
 * `EPERM` means it exists and this process may not signal it, which is still
 * alive; only `ESRCH` means gone.
 */
function processExists(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'EPERM';
  }
}

function ok(requestId: unknown, body: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    protocol_version: 1,
    request_id: requestId,
    timestamp: new Date().toISOString(),
    ok: true,
    ...body,
  };
}

function main(): void {
  const options = parseOptions(process.argv.slice(2));
  options.fixture = resolveFixture(options.fixture);
  // Canonicalized on the way in, the way the server canonicalizes on the way
  // out: every read path a client can reach translates legacy lifecycle events,
  // so the replay owes the TUI the translated stream, not the recorded one.
  const events = canonicalJournalEvents(readJournalRecords(options.fixture));
  const experiments = loadExperiments(options.fixture);
  const replay = new Replay(events, options);
  process.stderr.write(
    `mock: ${String(events.length)} events from ${options.fixture}\n` +
      `mock: speed x${String(options.speed)}` +
      `${options.startPaused ? ' (paused; /resume in the TUI to start)' : ''}\n`,
  );

  const server = createServer(socket => {
    readLines(socket, request => {
      const id = request['request_id'];
      const type = request['type'];
      switch (type) {
        case 'subscribe': {
          // Order matters: `subscribed` resolves the client's promise, and the
          // bootstrap batch must follow it on the same connection.
          replay.addSubscriber(socket);
          // Prime first: `latest_sequence` and the bootstrap batch must both
          // describe the same set of events.
          replay.primeBootstrap();
          writeLine(socket, {
            type: 'subscribed',
            request_id: id,
            run_id: replay.runId,
            latest_sequence: replay.latestSequence,
          });
          writeLine(socket, {
            type: 'event_batch',
            events: replay.delivered,
            through_sequence: replay.latestSequence,
            active_executions: replay.activeExecutions,
            store_id: replay.storeId,
            // 0 means the stream carries its whole history, so the TUI never
            // asks for a backfill it cannot get.
            history_after_sequence: 0,
          });
          replay.start();
          return;
        }
        case 'query.snapshot': {
          const last = replay.delivered.at(-1);
          const template = responseBody(type)['snapshot'];
          writeLine(
            socket,
            ok(id, {
              snapshot: withValues(template, {
                run_id: replay.runId,
                sequence: replay.latestSequence,
                status: replayRunStatus(replay.delivered),
                agent_kind: last?.agent_kind ?? null,
                round_label: last?.round_label ?? null,
                // Boot queries the snapshot and subscribes concurrently, so
                // this answer can land after the bootstrap batch at the same
                // sequence, where the client accepts it. Left static and empty
                // it then erased an execution the batch had just opened.
                active_executions: replay.activeExecutions,
              }),
            }),
          );
          return;
        }
        case 'query.tui_defaults': {
          const theme = process.env['VIBESYS_THEME'];
          const template = responseBody(type)['tui_defaults'];
          writeLine(
            socket,
            ok(id, {
              tui_defaults: withValues(template, theme === undefined ? {} : {theme}),
            }),
          );
          return;
        }
        case 'query.experiments': {
          writeLine(
            socket,
            ok(id, withValues(responseBody(type), {experiments, experiments_ready: true})),
          );
          return;
        }
        case 'query.events': {
          const after = Number(request['after_sequence'] ?? 0);
          const beforeRaw = request['before_sequence'];
          const before = typeof beforeRaw === 'number' ? beforeRaw : null;
          writeLine(
            socket,
            ok(id, withValues(responseBody(type), {events: replay.eventsInRange(after, before)})),
          );
          return;
        }
        case 'query.performance': {
          writeLine(socket, ok(id, responseBody(type)));
          return;
        }
        case 'query.chat_options': {
          writeLine(socket, ok(id, responseBody(type)));
          return;
        }
        case 'query.chat_thread_create': {
          writeLine(socket, ok(id, responseBody(type)));
          return;
        }
        case 'query.chat': {
          // Answered on its own connection, which the client ends afterward.
          // `ChatResult` echoes the question, so the mock does too rather than
          // sending a reply that claims nothing was asked.
          const question = request['question'];
          writeLine(
            socket,
            ok(id, {
              chat: withValues(responseBody(type)['chat'], {
                question: typeof question === 'string' ? question : '',
              }),
            }),
          );
          return;
        }
        // The run controls double as replay controls, so the replay is driven
        // from inside the TUI with the real keybindings.
        // `CommandAck` is {action, status} with status in `pending | consumed`.
        // Anything else renders in the client as `undefined: <status>`.
        case 'command.pause': {
          replay.pause();
          writeLine(socket, ok(id, responseBody(type)));
          return;
        }
        case 'command.resume': {
          replay.resume();
          writeLine(socket, ok(id, responseBody(type)));
          return;
        }
        case 'command.steer': {
          writeLine(socket, ok(id, responseBody(type)));
          return;
        }
        default: {
          writeLine(socket, {
            protocol_version: 1,
            request_id: id,
            ok: false,
            error: `mock server does not implement ${String(type)}`,
          });
        }
      }
    });
  });

  try {
    unlinkSync(options.socketPath);
  } catch {
    // No stale socket to clear.
  }
  server.listen(options.socketPath, () => {
    process.stderr.write(`mock: listening on ${options.socketPath}\n`);
  });

  /**
   * The server exists to feed one client, so it exits when that client goes.
   *
   * Relying on the launching shell to kill it does not work: tmux kills a pane
   * without giving bash a chance to run an EXIT trap while a foreground child
   * is running, which orphaned the server, its socket, and its log on every
   * `tmux kill-session`. Watching the subscription is independent of how the
   * client died.
   */
  let exitTimer: ReturnType<typeof setTimeout> | null = null;
  replay.onSubscriberArrived = () => {
    if (exitTimer === null) return;
    clearTimeout(exitTimer);
    exitTimer = null;
  };
  replay.onLastSubscriberGone = () => {
    if (exitTimer !== null) clearTimeout(exitTimer);
    exitTimer = setTimeout(() => {
      process.stderr.write('mock: client disconnected, exiting\n');
      shutdown();
    }, DISCONNECT_GRACE_MS);
  };

  const shutdown = (): void => {
    server.close();
    try {
      unlinkSync(options.socketPath);
    } catch {
      // Already gone.
    }
    // Only a log this run generated. A path the caller asked for is theirs.
    const ownedLog = process.env['VS_MOCK_OWNED_LOG'];
    if (ownedLog !== undefined && ownedLog !== '') {
      try {
        unlinkSync(ownedLog);
      } catch {
        // Already gone.
      }
    }
    process.exit(0);
  };

  /**
   * The subscription is not enough on its own: `onLastSubscriberGone` only ever
   * fires for a client that subscribed at least once. A client that dies
   * between the bind and its first `subscribe`, on a missing runtime or an
   * exception during TUI initialisation, left this process adopted by PID 1
   * with its socket and log still on disk, and nothing upstream could reach it
   * because `mock-ui.sh` has already `exec`ed the client over its own shell.
   *
   * Watching the owner covers that window and every later one, whatever killed
   * the client. `mock-ui.sh` passes the pid the client will run under, which is
   * the launching shell's own pid because it `exec`s.
   */
  if (options.ownerPid !== null) {
    const ownerPid = options.ownerPid;
    const watchdog = setInterval(() => {
      if (processExists(ownerPid)) return;
      process.stderr.write(`mock: client process ${String(ownerPid)} is gone, exiting\n`);
      shutdown();
    }, OWNER_POLL_MS);
    watchdog.unref?.();
  }

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
  // SIGHUP is what arrives first when the terminal or tmux session the client
  // was drawing to goes away, ahead of the disconnect grace period.
  process.on('SIGHUP', shutdown);
}

main();
