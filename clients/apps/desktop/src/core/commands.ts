/**
 * The user's commands to the host. Each user action mints one id and sends one Dispatch. A reply
 * that cannot be told (a lost link, a timeout) leaves the command `Unconfirmed`: the app never
 * re-sends it. It settles when the receipt for that id arrives on the run stream, or when the
 * user retries, which sends the same command under the same id so the daemon answers with the
 * receipt it already holds.
 */
import {create} from '@bufbuild/protobuf';
import {type Command, CommandSchema, oneofIs} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Atom from 'effect/reactivity/Atom';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {answered, type CommandProgress, settle} from '../state/index.js';
import {type AppError, InvalidMessage, isRetryable} from './errors.js';
import {faultOf} from './fault.js';
import type {HostParts} from './host-store.js';
import {Ids} from './ids.js';
import {COMMAND_DEADLINE} from './policy.js';

/** What the user asked for: the body of a command, without an id. */
export type Intent = Extract<
  Command['body'],
  {
    readonly case:
      | 'startRun'
      | 'stopRun'
      | 'stopAgent'
      | 'sendMessage'
      | 'requestCatchUp'
      | 'updateRun';
  }
>;

export interface Commands {
  /**
   * Sends `intent` once under a fresh id and answers that id at once; `progress` follows it. It
   * never fails: a failure is the command's progress.
   */
  readonly submit: (intent: Intent) => Effect.Effect<string>;
  /**
   * The user retries a command that is `Unconfirmed`, with the same id. Anything else (settled,
   * unknown, still sending) is left alone.
   */
  readonly retry: (commandId: string) => Effect.Effect<void>;
  /** Where a command stands; `undefined` for an id this session never issued. */
  readonly progress: (commandId: string) => Atom.Atom<CommandProgress | undefined>;
  /** Where a command stands at this moment, as `progress` would say it. */
  readonly now: (commandId: string) => Effect.Effect<CommandProgress | undefined>;
}

type Entries = Readonly<Record<string, CommandProgress>>;

/** Whether the command may have reached the host even though the call failed. */
const maybeApplied = (error: AppError): boolean =>
  isRetryable(error) || (error._tag === 'InvalidMessage' && error.direction === 'response');

const failedWith = (command: Command, error: AppError): CommandProgress => {
  const fault = faultOf(error);
  return maybeApplied(error)
    ? {_tag: 'Unconfirmed', command, fault}
    : {_tag: 'Failed', command, fault};
};

const noReceipt = new InvalidMessage({
  direction: 'response',
  type: 'DispatchResponse',
  detail: 'receipt is missing',
});

/**
 * A run is not started on a host that says it cannot run one (a part it needs is missing or too
 * old): that is refused here, in the host's typed words, before any agent is launched. The host
 * checks the same when the start reaches it; this is the answer that names the cause. Nothing but a
 * start is held back by it.
 */
const refusedStart = (parts: HostParts, command: Command) =>
  oneofIs(command.body, 'startRun', {
    startRun: true,
    stopRun: true,
    spawnAgent: true,
    stopAgent: true,
    sendMessage: true,
    postHandoff: true,
    postReport: true,
    requestCatchUp: true,
    updateRun: true,
    beginSetup: true,
    completeSetup: true,
    declareTask: true,
  })
    ? parts.connection.blocked
    : Effect.succeed(undefined);

/**
 * Waits for a Ready connection (a command is not sent into the void), then sends once. A call
 * that failed because of the link tells the connection, so a dropped link is noticed even when no
 * feed is mounted to see it.
 */
const dispatchOnce = (parts: HostParts, command: Command) =>
  Effect.gen(function* () {
    const ready = yield* parts.connection.ready().pipe(Effect.result);
    if (ready._tag === 'Failure') {
      const {state} = ready.failure;
      const detail =
        state._tag === 'Incompatible' ? 'the host runs an incompatible build' : state.fault.detail;
      return {
        _tag: 'Failed',
        command,
        fault: {kind: 'Unavailable', detail, retryable: false},
      } as const;
    }
    const epoch = ready.success;
    const blocked = yield* refusedStart(parts, command);
    if (blocked !== undefined) return failedWith(command, blocked);
    const sent = yield* parts.client
      .dispatch({command}, {deadline: COMMAND_DEADLINE})
      .pipe(Effect.result);
    if (sent._tag === 'Failure') {
      if (isRetryable(sent.failure)) yield* parts.connection.suspect(epoch, sent.failure);
      return failedWith(command, sent.failure);
    }
    const {receipt} = sent.success;
    return receipt === undefined ? failedWith(command, noReceipt) : answered(receipt);
  });

export const makeCommands = Effect.fn('Commands.make')(function* (parts: HostParts) {
  const ids = yield* Ids;
  const scope = yield* Effect.scope;
  const entries = yield* SubscriptionRef.make<Entries>({});
  const entriesAtom = Atom.keepAlive(Atom.subscriptionRef(entries));

  const record = (id: string, progress: CommandProgress) =>
    SubscriptionRef.update(entries, all => ({...all, [id]: progress}));

  /** Sends `command` in the background so a screen going away does not abandon it. */
  const send = (command: Command) =>
    dispatchOnce(parts, command).pipe(
      Effect.flatMap(progress => record(command.id, progress)),
      Effect.provideContext(parts.ambient),
      Effect.forkIn(scope),
      Effect.asVoid,
    );

  const submit = Effect.fn('Commands.submit')(function* (intent: Intent) {
    const id = yield* ids.next;
    const command = create(CommandSchema, {id, body: intent});
    yield* record(id, {_tag: 'Sending', command});
    yield* send(command);
    return id;
  });

  const retry = Effect.fn('Commands.retry')(function* (commandId: string) {
    const {receipts} = (yield* SubscriptionRef.get(parts.store.ref)).data;
    // Deciding and recording Sending is one step, so two retries of one command send it once.
    const command = yield* SubscriptionRef.modify(entries, (all): [Command | null, Entries] => {
      const held = all[commandId];
      const now = held === undefined ? undefined : settle(held, receipts);
      return now?._tag === 'Unconfirmed'
        ? [now.command, {...all, [commandId]: {_tag: 'Sending', command: now.command}}]
        : [null, all];
    });
    if (command !== null) yield* send(command);
  });

  const progress = Atom.family((commandId: string) =>
    Atom.make(get => {
      const held = get(entriesAtom)[commandId];
      return held === undefined ? undefined : settle(held, get(parts.store.atom).data.receipts);
    }),
  );

  const now = Effect.fn('Commands.now')(function* (commandId: string) {
    const held = (yield* SubscriptionRef.get(entries))[commandId];
    const {receipts} = (yield* SubscriptionRef.get(parts.store.ref)).data;
    return held === undefined ? undefined : settle(held, receipts);
  });

  const commands: Commands = {submit, retry, progress, now};
  return commands;
});
