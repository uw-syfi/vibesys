/**
 * The fixture source: a `FakeDaemon` behind the `OrchestratorSource` interface. A stream follows
 * the daemon until its caller cancels, and `dropsAfter` scripts the connection to fail after that
 * many responses on each successive connection, so a reconnect is a fixture too.
 */
import {create} from '@bufbuild/protobuf';
import {
  type Command,
  type Event,
  type ItemsPage,
  type Plan,
  type Receipt,
  type WatchAgentResponse,
  WatchAgentResponseSchema,
  type WatchRunsResponse,
} from '@vibesys/orchestrator-proto';
import {
  type CancelSignal,
  type OrchestratorSource,
  SourceError,
} from '@vibesys/orchestrator-source';
import type {FakeDaemon} from './fake-daemon.js';

export interface FixtureSourceOptions {
  /** After this many responses on connection 1, 2, ...; a connection past the list does not fail. */
  readonly dropsAfter?: readonly number[];
}

/** Waits until `wake` is called by the daemon or the caller cancels. */
function wakeOnChange(daemon: FakeDaemon, signal: CancelSignal): Promise<void> {
  return new Promise(resolve => {
    const stop = daemon.watch(() => {
      stop();
      resolve();
    });
    signal.addEventListener(
      'abort',
      () => {
        stop();
        resolve();
      },
      {once: true},
    );
  });
}

export class FixtureSource implements OrchestratorSource {
  #connections = 0;

  constructor(
    readonly daemon: FakeDaemon,
    private readonly options: FixtureSourceOptions = {},
  ) {}

  /** The number of connections opened so far. */
  get connections(): number {
    return this.#connections;
  }

  #budget(): number {
    const drops = this.options.dropsAfter ?? [];
    const budget = drops[this.#connections];
    this.#connections += 1;
    return budget ?? Number.POSITIVE_INFINITY;
  }

  /**
   * A stream that opens with `open()` when the caller has seen nothing, then follows the daemon:
   * `pick` turns each committed event into a response, or null to skip it. Each response spends
   * the connection's budget; at zero the connection drops.
   */
  async *#follow<T>(
    afterSequence: bigint,
    signal: CancelSignal,
    open: () => {readonly first: readonly T[]; readonly after: bigint},
    pick: (event: Event) => T | null,
  ): AsyncGenerator<T> {
    let budget = this.#budget();
    const spend = (): void => {
      if (budget <= 0) throw new SourceError('lost', 'the connection dropped');
      budget -= 1;
    };
    let after = afterSequence;
    if (after === 0n) {
      const opening = open();
      after = opening.after;
      for (const response of opening.first) {
        spend();
        yield response;
      }
    }
    while (!signal.aborted) {
      // Re-read after every wait and every yield: events may be committed while a response is
      // being consumed, and waiting then would sleep through them.
      const pending = this.daemon.eventsAfter(after);
      if (pending.length === 0) {
        await wakeOnChange(this.daemon, signal);
        continue;
      }
      for (const event of pending) {
        after = event.sequence;
        const response = pick(event);
        if (response === null) continue;
        spend();
        yield response;
      }
    }
  }

  watchRuns(
    afterSequence: bigint,
    signal: CancelSignal,
  ): AsyncGenerator<WatchRunsResponse['body']> {
    return this.#follow<WatchRunsResponse['body']>(
      afterSequence,
      signal,
      () => {
        const snapshot = this.daemon.snapshot();
        return {first: [{case: 'snapshot', value: snapshot}], after: snapshot.sequence};
      },
      event => ({case: 'event', value: event}),
    );
  }

  watchAgent(
    agentId: string,
    pageSize: number,
    afterSequence: bigint,
    signal: CancelSignal,
  ): AsyncGenerator<WatchAgentResponse> {
    const frame = (body: WatchAgentResponse['body'], sequence: bigint): WatchAgentResponse =>
      create(WatchAgentResponseSchema, {body, sequence});
    return this.#follow<WatchAgentResponse>(
      afterSequence,
      signal,
      () => {
        const page = this.daemon.page(agentId, pageSize, 0);
        const progress = this.daemon.scenario.deltas
          .filter(delta => delta.agentId === agentId)
          .map(value => frame({case: 'delta', value}, page.sequence));
        return {
          first: [frame({case: 'page', value: page}, page.sequence), ...progress],
          after: page.sequence,
        };
      },
      event => {
        const {body} = event;
        if (body.case === 'itemUpdated' && body.value.agentId === agentId)
          return frame({case: 'item', value: body.value}, event.sequence);
        if (body.case === 'turnUpdated' && body.value.agentId === agentId)
          return frame({case: 'turn', value: body.value}, event.sequence);
        return null;
      },
    );
  }

  listItems(
    agentId: string,
    beforeOrdinal: number,
    pageSize: number,
    _signal?: CancelSignal,
  ): Promise<ItemsPage> {
    return Promise.resolve(this.daemon.page(agentId, pageSize, beforeOrdinal));
  }

  listPlans(_runId?: string, _signal?: CancelSignal): Promise<readonly Plan[]> {
    return Promise.resolve(this.daemon.plans);
  }

  dispatch(command: Command, _signal?: CancelSignal): Promise<Receipt> {
    return Promise.resolve(this.daemon.dispatch(command));
  }
}
