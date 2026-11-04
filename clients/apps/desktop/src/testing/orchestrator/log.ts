/** A gap-free event log being written, with the builders for each kind of fact. */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  type Agent,
  type Event,
  EventSchema,
  type Item,
  type Message,
  type Receipt,
  type Run,
  type Turn,
} from '@vibesys/orchestrator-proto';

/** The generator's own seeded randomness (mulberry32), so a fixture is a function of its seed. */
export class Rng {
  #state: number;
  constructor(seed: number) {
    this.#state = seed >>> 0 || 1;
  }
  next(): number {
    this.#state = (this.#state + 0x6d2b79f5) >>> 0;
    let t = this.#state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }
  int(n: number): number {
    return Math.floor(this.next() * n);
  }
  between(low: number, high: number): number {
    return low + this.int(high - low + 1);
  }
  pick<T>(items: readonly T[]): T {
    return items[this.int(items.length)] as T;
  }
}

export type Body =
  | {case: 'runUpdated'; value: Run}
  | {case: 'agentUpdated'; value: Agent}
  | {case: 'turnUpdated'; value: Turn}
  | {case: 'itemUpdated'; value: Item}
  | {case: 'messageUpdated'; value: Message}
  | {case: 'receiptRecorded'; value: Receipt};

export class EventLog {
  readonly events: Event[] = [];
  constructor(readonly runId: string) {}

  /** Append one fact at `atMs`; the sequence is the next one. */
  push(atMs: number, body: Body, commandId = ''): Event {
    const event = create(EventSchema, {
      sequence: BigInt(this.events.length + 1),
      runId: this.runId,
      commandId,
      at: timestampFromMs(atMs),
      body,
    });
    this.events.push(event);
    return event;
  }
}
