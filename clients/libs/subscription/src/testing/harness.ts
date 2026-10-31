/** The machine over fake time and a scripted stream, for the tests of this package. */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {clockFrom} from '@vibesys/machine';
import {MachineStatsSchema, type TopicEntry, TopicEntrySchema} from '@vibesys/proto';
import {registerAbort} from '@vibesys/transport';
import {FakeClock} from '@vibesys/transport/testing';
import {type Actor, createActor, fromPromise} from 'xstate';
import type {DataUpdate} from '../fold.js';
import type {StreamEvent} from '../frames.js';
import {
  controlOf,
  type StreamOperation,
  type SubscriptionInput,
  subscriptionMachine,
} from '../machine.js';
import type {Control} from '../state.js';
import {type AnyTopic, machineStats, type Topic} from '../topics.js';

export function statsEntry(key: string, sampledMs: number): TopicEntry {
  return create(TopicEntrySchema, {
    key,
    value: {
      case: 'machineStats',
      value: create(MachineStatsSchema, {sampledAt: timestampFromMs(sampledMs)}),
    },
  });
}

/** A topic of several keys that only the lease bounds, like a run list. */
export const LEASE_ONLY: Topic<ReadonlyMap<string, TopicEntry>> = {
  ...machineStats,
  policy: {...machineStats.policy, cadenceMs: 0, staleAfterMs: 0, maxKeys: 8},
  fold: entries => entries,
};

/** One stream the machine opened: the test is the gateway. */
export class FakeStream {
  aborted = false;
  settled = false;
  constructor(
    private readonly operation: StreamOperation,
    private readonly settle: {resolve(): void; reject(error: unknown): void},
    private readonly now: () => number,
  ) {}

  send(event: StreamEvent): void {
    this.operation.receive(event);
  }
  /** The stream ends the way a closed connection does. */
  fail(): void {
    this.settled = true;
    this.settle.reject(new Error('stream failed'));
  }
  finish(): void {
    this.settled = true;
    this.settle.resolve();
  }
  reach(step: 'gateway' | 'handshake' | 'stream'): void {
    this.send({type: 'reached', step});
  }
  /** Frames as the shell stamps them: with the time they arrived. */
  beat(): void {
    this.send({type: 'beat', at: this.now()});
  }
  entry(entry: TopicEntry): void {
    this.send({type: 'entry', entry, at: this.now()});
  }
  removed(key: string): void {
    this.send({type: 'removed', key, at: this.now()});
  }
  synced(): void {
    this.send({type: 'synced', at: this.now()});
  }
  /** The first frames: open, then the snapshot. */
  snapshot(entries: readonly TopicEntry[]): void {
    this.reach('stream');
    for (const entry of entries) this.entry(entry);
    this.synced();
  }
}

export class Harness {
  readonly clock = new FakeClock();
  readonly streams: FakeStream[] = [];
  readonly updates: DataUpdate[] = [];
  readonly controls: Control[] = [];
  readonly actor: Actor<typeof subscriptionMachine>;

  constructor(
    readonly topic: AnyTopic = machineStats,
    openBudgetMs = 60_000,
  ) {
    const input: SubscriptionInput = {topic, openBudgetMs};
    this.actor = createActor(
      subscriptionMachine.provide({
        actors: {
          stream: fromPromise<void, StreamOperation>(({input: operation, signal}) => {
            return new Promise<void>((resolve, reject) => {
              const stream = new FakeStream(
                {
                  // A stream that was aborted may still speak; the shell drops what it says.
                  receive: event => {
                    if (!signal.aborted) operation.receive(event);
                  },
                },
                {resolve, reject},
                () => this.clock.now(),
              );
              registerAbort(signal, () => {
                stream.aborted = true;
              });
              this.streams.push(stream);
            });
          }),
        },
      }),
      {input, clock: clockFrom(this.clock.schedule)},
    );
    this.actor.on('update', event => this.updates.push(event.update));
    this.actor.subscribe(snapshot => {
      const control = controlOf(snapshot);
      if (JSON.stringify(this.controls.at(-1)) !== JSON.stringify(control))
        this.controls.push(control);
    });
    this.actor.start();
  }

  get control(): Control {
    return controlOf(this.actor.getSnapshot());
  }
  /** The streams the machine holds open now. */
  get open(): readonly FakeStream[] {
    return this.streams.filter(stream => !stream.aborted && !stream.settled);
  }
  get latest(): FakeStream {
    const stream = this.streams.at(-1);
    if (stream === undefined) throw new Error('the machine opened no stream');
    return stream;
  }
}
