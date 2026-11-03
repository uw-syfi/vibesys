import {create} from '@bufbuild/protobuf';
import {
  type Agent,
  AgentRole,
  CommandSchema,
  Delivery,
  Harness,
  JobOutcome,
  MessageSchema,
  ReceiptSchema,
  Refusal,
  Rejection,
  RunStatus,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe, expect, it} from 'vitest';
import {
  applyEvent,
  type CommandProgress,
  emptyState,
  type OrchestratorState,
  type RunView,
} from '../../state/index.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {AgentPanel} from './agent-panel.js';
import {ConnectionBanner, type ConnectionNote} from './connection-banner.js';
import {HandoffCards, verdictOf} from './handoff-cards.js';
import {RunList} from './run-list.js';
import {RunScreen} from './run-screen.js';
import {describeArgs, harnessWord, toolLabel} from './words.js';

const VIEWS: readonly RunView[] = ['timeline', 'cost', 'handoffs'];
/** Names of the protocol that must never reach a person. */
const PROTOCOL = /UNSPECIFIED|AGENT_ROLE|TURN_|ITEM_STATUS|undefined|\[object/;

function stateOf(seed: number, subagents: number, live: boolean) {
  const scenario = generateScenario({seed, subagents, live, depth: 2});
  const state = scenario.events.reduce(applyEvent, emptyState);
  return {scenario, state, runId: scenario.runId};
}

function screen(
  state: OrchestratorState,
  runId: string,
  view: RunView,
  nowMs: number,
  stopProgress?: CommandProgress,
): string {
  return renderToStaticMarkup(
    <RunScreen
      state={state}
      host="host-a"
      runId={runId}
      nowMs={nowMs}
      view={view}
      agentId={null}
      agentPanel={null}
      stopProgress={stopProgress}
      stop={() => undefined}
      retryStop={() => undefined}
      goRuns={() => undefined}
      show={() => undefined}
      pick={() => undefined}
    />,
  );
}

function panel(state: OrchestratorState, agent: Agent, sendProgress?: CommandProgress): string {
  return renderToStaticMarkup(
    <AgentPanel
      state={state}
      agent={agent}
      name="Worker 1"
      sendProgress={sendProgress}
      send={() => undefined}
      retrySend={() => undefined}
      close={() => undefined}
    />,
  );
}

describe('the screens', () => {
  it('every view of any run, live or finished, renders and names nothing from the protocol', () => {
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

  it('the run list shows runs of every host and says when a host is not connected', () => {
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

  it('a connection that is not working says so with the reason and a Retry, and a working one is silent', () => {
    const fault = {kind: 'TransportFailed', detail: 'ssh exited', retryable: true} as const;
    const note = (
      connection: ConnectionNote['connection'],
      feed: ConnectionNote['feed'],
    ): ConnectionNote => ({
      key: 'runs',
      what: 'the runs on host-b',
      connection,
      feed,
      hasData: true,
      tryAgain: () => undefined,
    });
    const ready = {_tag: 'Ready', epoch: 1, daemon: undefined as never} as const;
    const html = (n: ConnectionNote) => renderToStaticMarkup(<ConnectionBanner notes={[n]} />);
    expect(html(note(ready, {_tag: 'Live'}))).toBe('');
    expect(html(note(ready, {_tag: 'Opening'}))).toContain('Catching up');
    for (const lost of [
      note({_tag: 'Waiting', attempt: 1, fault, retryAtMs: 5}, {_tag: 'Live'}),
      note(ready, {_tag: 'Failed', fault}),
      note({_tag: 'Unusable', fault}, {_tag: 'Opening'}),
    ]) {
      expect(html(lost)).toContain('Retry');
      expect(html(lost)).toContain('out of date');
      expect(html(lost)).toContain('ssh exited');
    }
    expect(html(note({_tag: 'Incompatible', side: 'daemon-too-old'}, {_tag: 'Opening'}))).toContain(
      'Update VibeSys on that host',
    );
  });

  it('the chip under the box follows the stored message, not only the first answer', () => {
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
    const progress: CommandProgress = {_tag: 'Accepted', receipt};
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
    expect(panel(state, agent, progress)).toContain('Sent, not yet confirmed');
    expect(panel(stored(Delivery.CONSUMED), agent, progress)).toContain('Read by the agent');
    const rejected = panel(stored(Delivery.REJECTED, Rejection.AGENT_GONE), agent, progress);
    expect(rejected).toContain('Not delivered');
    expect(rejected).toContain('session is over');
  });

  it('a command shows as the command, never as the JSON the tool received', () => {
    expect(describeArgs('{"command":["bash","-lc","ls -la"]}')).toBe('bash -lc ls -la');
    expect(describeArgs('{"cmd":"make test"}')).toBe('make test');
    expect(describeArgs('{"path":"a.ts","line":3}')).toBe('path: a.ts\nline: 3');
    expect(toolLabel('exec_command')).toBe('Command');
    expect(toolLabel('some_custom_tool')).toBe('Some custom tool');
  });

  it('a Claude and a Codex agent say, in their own words, what a message can do', () => {
    const {state} = stateOf(5, 4, true);
    const agents = Object.values(state.agents);
    const claude = agents.find(a => a.capabilities?.steers === true && a.role === AgentRole.WORKER);
    const codex = agents.find(a => a.capabilities?.steers === false && a.role === AgentRole.WORKER);
    if (claude === undefined || codex === undefined)
      throw new Error('the fixture mixes both tools');
    expect(panel(state, claude)).toContain('in the middle of what it is doing');
    expect(panel(state, codex)).toContain('waits until it finishes its current step');
  });

  it('a handoff shows its benchmark job outcome as the verdict, and none when unknown', () => {
    const {state} = stateOf(5, 4, false);
    expect(verdictOf(state, 'nobody')).toBeNull();
    const finished = create(MessageSchema, {
      id: 'job-1',
      body: {
        case: 'jobFinished',
        value: {submittedByAgentId: 'worker-1', outcome: JobOutcome.SUCCEEDED},
      },
    });
    const withJob = {...state, messages: {...state.messages, 'job-1': finished}};
    expect(verdictOf(withJob, 'worker-1')?.label).toBe('Its benchmark job: Succeeded');
  });

  it('every agent row carries its harness chip and its id, so a reader needs no detail view', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 30}),
        fc.integer({min: 1, max: 8}),
        (seed, subagents) => {
          const {scenario, state, runId} = stateOf(seed, subagents, true);
          const html = screen(state, runId, 'timeline', scenario.endMs + 60_000);
          const rows = html.split('role="treeitem"').slice(1);
          const agents = Object.values(state.agents).filter(agent => agent.runId === runId);
          expect(rows).toHaveLength(agents.length);
          for (const agent of agents) {
            const row = rows.find(candidate => candidate.includes(`data-row-id="${agent.id}"`));
            expect(row).toBeDefined();
            expect(row).toContain(harnessWord(agent.harness));
          }
        },
      ),
      {numRuns: 20},
    );
  });

  it('shows Stop only while the run can be stopped, and what became of the press', () => {
    const {scenario, state, runId} = stateOf(3, 2, true);
    const run = state.runs[runId];
    if (run === undefined) throw new Error('the fixture has its run');
    const as = (status: RunStatus): OrchestratorState => ({
      ...state,
      runs: {...state.runs, [runId]: {...run, status}},
    });
    const now = scenario.endMs + 60_000;
    for (const [status, shown] of [
      [RunStatus.STARTING, true],
      [RunStatus.RUNNING, true],
      [RunStatus.STOPPING, false],
      [RunStatus.ENDED, false],
      [RunStatus.FAILED, false],
    ] as const) {
      expect(screen(as(status), runId, 'timeline', now).includes('Stop run')).toBe(shown);
    }
    const command = create(CommandSchema, {id: 'c'});
    const sending = screen(as(RunStatus.RUNNING), runId, 'timeline', now, {
      _tag: 'Sending',
      command,
    });
    expect(sending).toContain('Stop: sending.');
    const refused = screen(as(RunStatus.RUNNING), runId, 'timeline', now, {
      _tag: 'Refused',
      receipt: create(ReceiptSchema, {commandId: 'c', refusal: Refusal.RUN_NOT_RUNNING}),
    });
    expect(refused).toContain('The run is not running.');
    const unsure = screen(as(RunStatus.RUNNING), runId, 'timeline', now, {
      _tag: 'Unconfirmed',
      command,
      fault: {kind: 'DeadlineExceeded', detail: 'waited 15s', retryable: true},
    });
    expect(unsure).toContain('not confirmed');
    expect(unsure).toContain('Try again');
    expect(unsure).toContain('waited 15s');
  });

  it('the final report is a named region whose first line is the lead\u2019s headline', () => {
    const report = create(MessageSchema, {
      id: 'report-1',
      runId: 'r',
      body: {
        case: 'report',
        value: {
          headline: 'best score 17 at abc1234',
          changed: 'a',
          decisions: 'b',
          openForUser: '',
        },
      },
    });
    const state = {...emptyState, messages: {'report-1': report}};
    const html = renderToStaticMarkup(<HandoffCards state={state} runId="r" />);
    expect(html).toContain('aria-labelledby');
    expect(html).toContain('Final report');
    expect(html).toContain('best score 17 at abc1234');
  });

  it('the lead and a subagent on another tool are told apart by their chips', () => {
    expect(harnessWord(Harness.CLAUDE_CODE)).toBe('Claude Code');
    expect(harnessWord(Harness.CODEX)).toBe('Codex');
  });
});
