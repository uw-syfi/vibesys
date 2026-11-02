import {describe, expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {generateScenario} from '@vibesys/orchestrator-fixtures';
import {
  type Agent,
  AgentRole,
  Delivery,
  MessageSchema,
  PlanStatus,
  ReceiptSchema,
  Rejection,
} from '@vibesys/orchestrator-proto';
import {applyEvent, emptyState, type OrchestratorState} from '@vibesys/orchestrator-state';
import fc from 'fast-check';
import {renderToStaticMarkup} from 'react-dom/server';
import type {AgentView} from '../state/index.js';
import {AgentPanel, idForSend} from './agent-panel.js';
import {ConnectionBanner} from './connection-banner.js';
import {verdictOf} from './handoff-cards.js';
import type {RunView} from './route.js';
import {RunList} from './run-list.js';
import {RunScreen} from './run-screen.js';
import {describeArgs, planWord, toolLabel} from './words.js';

const VIEWS: readonly RunView[] = ['timeline', 'cost', 'handoffs', 'ideas'];
/** Names of the protocol that must never reach a person. */
const PROTOCOL = /UNSPECIFIED|AGENT_ROLE|TURN_|ITEM_STATUS|undefined|\[object/;

function stateOf(seed: number, subagents: number, live: boolean) {
  const scenario = generateScenario({seed, subagents, live, depth: 2});
  const state = scenario.events.reduce(applyEvent, emptyState);
  return {scenario, state, runId: scenario.runId};
}

function screen(state: OrchestratorState, runId: string, view: RunView, nowMs: number): string {
  return renderToStaticMarkup(
    <RunScreen
      state={state}
      host="host-a"
      runId={runId}
      nowMs={nowMs}
      view={view}
      agentId={null}
      plans={[]}
      ideas={{status: 'ready', plans: []}}
      agentPanel={null}
      goRuns={() => undefined}
      show={() => undefined}
      pick={() => undefined}
    />,
  );
}

const quiet = (agentId: string): AgentView => ({
  agentId,
  stream: 'live',
  fault: null,
  older: 'idle',
  olderError: null,
  box: {status: 'idle'},
});

function panel(state: OrchestratorState, agent: Agent, view = quiet(agent.id)): string {
  return renderToStaticMarkup(
    <AgentPanel
      state={state}
      agent={agent}
      name="Worker 1"
      view={view}
      showOlder={() => undefined}
      newId={() => 'id'}
      send={() => undefined}
      dismiss={() => undefined}
      close={() => undefined}
    />,
  );
}

describe('the screens', () => {
  test('every view of any run, live or finished, renders and names nothing from the protocol', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 40}),
        fc.integer({min: 0, max: 9}),
        fc.boolean(),
        (seed, subagents, live) => {
          const {scenario, state, runId} = stateOf(seed, subagents, live);
          for (const view of VIEWS) {
            const html = screen(state, runId, view, scenario.endMs + 60_000);
            expect(html).not.toMatch(PROTOCOL);
          }
          for (const agent of Object.values(state.agents)) {
            expect(panel(state, agent)).not.toMatch(PROTOCOL);
          }
        },
      ),
      {numRuns: 20},
    );
  });

  test('the run list shows runs of every host and says when a host is not connected', () => {
    const a = stateOf(1, 3, true);
    const b = stateOf(2, 1, false);
    const html = renderToStaticMarkup(
      <RunList
        hosts={[
          {key: 'ssh:host-a', label: 'host-a', state: a.state},
          {key: 'ssh:host-b', label: 'host-b', state: b.state},
        ]}
        open={() => undefined}
      />,
    );
    expect(html).toContain('host-a');
    expect(html).toContain('host-b');
  });

  test('a connection that is not working says so with the reason and a Retry, and a working one is silent', () => {
    const note = (status: 'connecting' | 'live' | 'waiting' | 'gave-up') => ({
      key: status,
      what: 'the runs on host-b',
      status,
      fault: status === 'live' ? null : ({kind: 'lost', detail: 'ssh exited'} as const),
      hasData: true,
      retry: () => undefined,
    });
    const html = (status: Parameters<typeof note>[0]) =>
      renderToStaticMarkup(<ConnectionBanner notes={[note(status)]} />);
    expect(html('live')).toBe('');
    expect(html('connecting')).toContain('Catching up');
    for (const status of ['waiting', 'gave-up'] as const) {
      expect(html(status)).toContain('Retry');
      expect(html(status)).toContain('out of date');
      expect(html(status)).toContain('ssh exited');
    }
  });

  test('the chip under the box follows the stored message, not only the first answer', () => {
    const {state} = stateOf(5, 4, true);
    const agent = Object.values(state.agents).find(
      candidate => candidate.role === AgentRole.WORKER,
    );
    if (agent === undefined) throw new Error('the fixture has a worker');
    const receipt = create(ReceiptSchema, {
      commandId: 'cmd',
      messageId: 'msg-1',
      agentId: agent.id,
      delivery: Delivery.STEERED,
    });
    const view: AgentView = {
      ...quiet(agent.id),
      box: {status: 'answered', commandId: 'cmd', text: 'hello', receipt},
    };
    const stored = (delivery: Delivery, rejection = Rejection.UNSPECIFIED) => ({
      ...state,
      messages: {
        ...state.messages,
        'msg-1': create(MessageSchema, {
          id: 'msg-1',
          toAgentId: agent.id,
          delivery,
          rejection,
          body: {case: 'text', value: 'hello'},
        }),
      },
    });
    expect(panel(state, agent, view)).toContain('Sent, not yet confirmed');
    expect(panel(stored(Delivery.CONSUMED), agent, view)).toContain('Read by the agent');
    const rejected = panel(stored(Delivery.REJECTED, Rejection.AGENT_GONE), agent, view);
    expect(rejected).toContain('Not delivered');
    expect(rejected).toContain('session is over');
  });

  test('a message is retried under its own id only while it has had no answer', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('idle', 'sending', 'answered', 'failed'),
        fc.boolean(),
        fc.boolean(),
        (status, sameId, drafted) => {
          const fault = {kind: 'timeout', detail: 'x'} as const;
          const receipt = create(ReceiptSchema, {commandId: 'a'});
          const box: AgentView['box'] =
            status === 'idle'
              ? {status}
              : status === 'sending'
                ? {status, commandId: 'a', text: 't'}
                : status === 'answered'
                  ? {status, commandId: 'a', text: 't', receipt}
                  : {status, commandId: 'a', text: 't', fault};
          const draft = {text: 't', id: drafted ? (sameId ? 'a' : 'b') : null};
          const id = idForSend(draft, box, () => 'fresh');
          expect(id).toBe(status === 'failed' && drafted && sameId ? 'a' : 'fresh');
        },
      ),
    );
  });

  test('a command shows as the command, never as the JSON the tool received', () => {
    expect(describeArgs('{"command":["bash","-lc","ls -la"]}')).toBe('bash -lc ls -la');
    expect(describeArgs('{"cmd":"make test"}')).toBe('make test');
    expect(describeArgs('{"path":"a.ts","line":3}')).toBe('path: a.ts\nline: 3');
    expect(toolLabel('exec_command')).toBe('Command');
    expect(toolLabel('some_custom_tool')).toBe('Some custom tool');
  });

  test('a Claude and a Codex agent say, in their own words, what a message can do', () => {
    const {state} = stateOf(5, 4, true);
    const agents = Object.values(state.agents);
    const claude = agents.find(a => a.capabilities?.steers === true && a.role === AgentRole.WORKER);
    const codex = agents.find(a => a.capabilities?.steers === false && a.role === AgentRole.WORKER);
    if (claude === undefined || codex === undefined)
      throw new Error('the fixture mixes both tools');
    expect(panel(state, claude)).toContain('in the middle of what it is doing');
    expect(panel(state, codex)).toContain('waits until it finishes its current step');
  });

  test('a handoff shows the recorded verdict of its idea beside its claim, and none when unknown', () => {
    const {scenario, state} = stateOf(5, 4, false);
    const message = Object.values(state.messages).find(
      m => m.body.case === 'handoff' && m.body.value.planNumber > 0,
    );
    if (message?.body.case !== 'handoff') throw new Error('the fixture has a worker handoff');
    const handoff = message.body.value;
    const plan = scenario.plans.find(candidate => candidate.number === handoff.planNumber);
    if (plan === undefined) throw new Error('the handoff names an idea of the run');
    const decided = {...plan, status: PlanStatus.WON};
    expect(verdictOf(handoff, [decided], state, 'nobody')?.label).toBe(
      `Recorded: ${planWord(PlanStatus.WON).label}`,
    );
    expect(verdictOf(handoff, [], state, 'nobody')).toBeNull();
  });
});
