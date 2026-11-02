import {describe, expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {
  CommandSchema,
  Delivery,
  Refusal,
  Rejection,
  type WatchRunsResponse,
} from '@vibesys/orchestrator-proto';
import {GatewaySource, type OrchestratorSource, SourceError} from '@vibesys/orchestrator-source';
import {
  applyEvent,
  applySnapshot,
  emptyState,
  type OrchestratorState,
} from '@vibesys/orchestrator-state';
import {FakeDaemon, FakeGateway, FixtureSource, generateScenario} from './index.js';

/** Apply one response of the stream to what the client holds. */
function take(state: OrchestratorState, body: WatchRunsResponse['body']): OrchestratorState {
  if (body.case === 'snapshot') return applySnapshot(state, body.value);
  return body.case === 'event' ? applyEvent(state, body.value) : state;
}

/** Read a source the way a screen does: until it has caught up to `sequence`, redialling after a drop. */
async function readUntil(
  source: OrchestratorSource,
  daemon: FakeDaemon,
  sequence: bigint,
): Promise<{state: OrchestratorState; dials: number}> {
  let state = emptyState;
  let dials = 0;
  const stop = new AbortController();
  while (state.sequence < sequence) {
    dials += 1;
    try {
      for await (const body of source.watchRuns(state.sequence, stop.signal)) {
        state = take(state, body);
        if (state.sequence >= sequence) break;
        // The run goes on while the client reads.
        daemon.commit(3);
      }
    } catch (error) {
      if (!(error instanceof SourceError)) throw error;
      // Events happen while the client is away.
      daemon.commit(40);
    }
  }
  stop.abort();
  return {state, dials};
}

const scenario = generateScenario({seed: 5, subagents: 9, live: true});
const drops = {dropsAfter: [4, 30, 8]};
const sources: [string, (daemon: FakeDaemon) => OrchestratorSource][] = [
  ['the fixture source', daemon => new FixtureSource(daemon, drops)],
  [
    'the gateway source over a fake gateway',
    daemon => new GatewaySource(new FakeGateway(new FixtureSource(daemon, drops)), 'ssh:host-a'),
  ],
];

describe.each(sources)('%s', (_name, make) => {
  test('a client that redials after each drop ends with the whole run', async () => {
    const daemon = new FakeDaemon(scenario, 20);
    const {state, dials} = await readUntil(make(daemon), daemon, BigInt(scenario.events.length));
    expect(dials).toBeGreaterThan(2);
    const whole = scenario.events.reduce(applyEvent, emptyState);
    expect({runs: state.runs, agents: state.agents, turns: state.turns}).toEqual({
      runs: whole.runs,
      agents: whole.agents,
      turns: whole.turns,
    });
  });

  test('a message to a live agent is answered with a receipt, and the same id with the same one', async () => {
    const source = make(new FakeDaemon(scenario));
    const signal = new AbortController().signal;
    const agent = Object.values(scenario.events.reduce(applyEvent, emptyState).agents).find(
      candidate => candidate.status === 2,
    );
    const command = create(CommandSchema, {
      id: 'cmd-1',
      body: {case: 'sendMessage', value: {agentId: agent?.id ?? '', text: 'hello'}},
    });
    const first = await source.dispatch(command, signal);
    expect(first.refusal).toBe(Refusal.UNSPECIFIED);
    expect([Delivery.QUEUED, Delivery.STEERED]).toContain(first.delivery);
    expect(await source.dispatch(command, signal)).toEqual(first);
  });

  test('a message the host later rejects is stored as rejected, with the reason', async () => {
    const daemon = new FakeDaemon(scenario);
    const source = make(daemon);
    const signal = new AbortController().signal;
    const agent = Object.values(scenario.events.reduce(applyEvent, emptyState).agents).find(
      candidate => candidate.status === 2,
    );
    const receipt = await source.dispatch(
      create(CommandSchema, {
        id: 'cmd-3',
        body: {case: 'sendMessage', value: {agentId: agent?.id ?? '', text: 'hello'}},
      }),
      signal,
    );
    daemon.reject(receipt.messageId, Rejection.AGENT_GONE);
    const stored = daemon.snapshot().messages.find(message => message.id === receipt.messageId);
    expect(stored?.delivery).toBe(Delivery.REJECTED);
    expect(stored?.rejection).toBe(Rejection.AGENT_GONE);
  });

  test('a message to an unknown agent is refused by name', async () => {
    const receipt = await make(new FakeDaemon(scenario)).dispatch(
      create(CommandSchema, {
        id: 'cmd-2',
        body: {case: 'sendMessage', value: {agentId: 'nobody', text: 'hi'}},
      }),
      new AbortController().signal,
    );
    expect(receipt.refusal).toBe(Refusal.AGENT_NOT_FOUND);
  });

  test('older items page back to the first one', async () => {
    const source = make(new FakeDaemon(scenario));
    const agent = Object.keys(scenario.events.reduce(applyEvent, emptyState).agents)[1] ?? '';
    const signal = new AbortController().signal;
    let before = 0;
    let seen = 0;
    for (;;) {
      const page = await source.listItems(agent, before === 0 ? 1_000_000 : before, 2, signal);
      seen += page.items.length;
      if (page.nextBeforeOrdinal === 0) break;
      before = page.nextBeforeOrdinal;
    }
    expect(seen).toBeGreaterThan(2);
  });
});
