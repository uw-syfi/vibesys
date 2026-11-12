import {create} from '@bufbuild/protobuf';
import {
  type Agent,
  AgentRole,
  CommandSchema,
  Delivery,
  EditCallSchema,
  Harness,
  MessageSchema,
  OtherCallSchema,
  ReadCallSchema,
  ReceiptSchema,
  Refusal,
  Rejection,
  RunStatus,
  ShellCallSchema,
  type ToolCall,
  ToolCallSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe, expect, it} from 'vitest';
import {
  applyChange,
  type CommandProgress,
  emptyState,
  type OrchestratorState,
  runReport,
} from '../../state/index.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {AgentPage} from '../agents/agent-page.js';
import {type ConnectionNote, ConnectionStatus} from './connection-status.js';
import {agentNames} from './names.js';
import {ReportScreen} from './report/report-screen.js';
import {describeCall, harnessWord} from './words.js';

/** Names of the protocol that must never reach a person. */
const PROTOCOL = /UNSPECIFIED|AGENT_ROLE|TURN_|ITEM_STATUS|undefined|\[object/;

function stateOf(seed: number, subagents: number, live: boolean) {
  const scenario = generateScenario({seed, subagents, live, depth: 2});
  const state = scenario.changes.reduce(applyChange, emptyState);
  return {scenario, state, runId: scenario.runId};
}

function report(
  state: OrchestratorState,
  runId: string,
  nowMs: number,
  stopProgress?: CommandProgress,
): string {
  const summary = runReport(state, runId, nowMs);
  if (summary === undefined) throw new Error('the fixture has its run');
  return renderToStaticMarkup(
    <ReportScreen
      report={summary}
      host="host-a"
      ideasWindow={null}
      nowMs={nowMs}
      cost={state.costs[runId]}
      costFeed={{status: {_tag: 'Live'}, freshness: 'live'}}
      retryCost={() => undefined}
      stopProgress={stopProgress}
      stop={() => undefined}
      retryStop={() => undefined}
      openCatchUp={() => undefined}
      runAgain={() => undefined}
      goRuns={() => undefined}
      openAgents={() => undefined}
      openCost={() => undefined}
      names={agentNames(state, runId)}
      reading={null}
    />,
  );
}

function page(state: OrchestratorState, agent: Agent, sendProgress?: CommandProgress): string {
  return renderToStaticMarkup(
    <AgentPage
      nowMs={0}
      state={state}
      agent={agent}
      host="host-a"
      trail={[]}
      reading={null}
      history={{
        earlier: {_tag: 'Idle'},
        wholes: {},
        loadEarlier: () => undefined,
        expand: () => undefined,
      }}
      open={() => undefined}
      sendProgress={sendProgress}
      send={() => undefined}
      retrySend={() => undefined}
      stopProgress={undefined}
      stop={() => undefined}
      retryStop={() => undefined}
    />,
  );
}

describe('the screens', () => {
  it('the report and every agent of any run, live or finished, render and name nothing from the protocol', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 40}),
        fc.integer({min: 0, max: 9}),
        fc.boolean(),
        (seed, subagents, live) => {
          const {scenario, state, runId} = stateOf(seed, subagents, live);
          expect(report(state, runId, scenario.endMs + 60_000)).not.toMatch(PROTOCOL);
          for (const agent of Object.values(state.agents)) {
            expect(page(state, agent)).not.toMatch(PROTOCOL);
          }
        },
      ),
      {numRuns: 20},
    );
  });

  it('a connection that is not working says so in chips with a Retry, and a working one is silent', () => {
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
      staleForMs: 40_000,
      tryAgain: () => undefined,
    });
    const ready = {_tag: 'Ready', epoch: 1, daemon: undefined as never} as const;
    const html = (n: ConnectionNote) => renderToStaticMarkup(<ConnectionStatus notes={[n]} />);
    expect(html(note(ready, {_tag: 'Live'}))).toBe('');
    expect(html(note(ready, {_tag: 'Opening'}))).toContain('Catching up');
    for (const lost of [
      note({_tag: 'Waiting', attempt: 1, fault, retryAtMs: 5}, {_tag: 'Live'}),
      note(ready, {_tag: 'Failed', fault}),
      note({_tag: 'Unusable', fault}, {_tag: 'Opening'}),
    ]) {
      expect(html(lost)).toContain('Retry');
      expect(html(lost)).toMatch(/Stale|Unreachable/);
      // The reason, the context and the host's own words are on hover, not lines of the screen.
      expect(html(lost)).toContain('aria-description="');
      expect(html(lost)).toContain('ssh exited');
    }
    // How long what is shown has been out of date, as the host's freshness reports it.
    const waiting = note({_tag: 'Waiting', attempt: 1, fault, retryAtMs: 5}, {_tag: 'Live'});
    expect(html(waiting)).toContain('Stale 40s');
    expect(html({...waiting, staleForMs: null})).not.toContain('Stale ');
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
    expect(page(state, agent, progress)).toContain('Sent, unconfirmed');
    expect(page(stored(Delivery.CONSUMED), agent, progress)).toContain('Read');
    const rejected = page(stored(Delivery.REJECTED, Rejection.AGENT_GONE), agent, progress);
    expect(rejected).toContain('Not delivered');
    expect(rejected).toContain('Session over');
  });

  it('a call shows as what it did, from the typed call and never as JSON', () => {
    const shown = (call: ToolCall['call']) => describeCall(create(ToolCallSchema, {call}));
    expect(
      shown({case: 'shell', value: create(ShellCallSchema, {command: 'bash -lc ls -la'})}),
    ).toEqual({name: 'Command', asked: 'bash -lc ls -la'});
    expect(shown({case: 'read', value: create(ReadCallSchema, {target: 'a.ts'})})).toEqual({
      name: 'Read a file',
      asked: 'a.ts',
    });
    expect(
      shown({case: 'edit', value: create(EditCallSchema, {targets: ['a.ts', 'b.ts']})}),
    ).toEqual({name: 'Edited files', asked: 'a.ts\nb.ts'});
    expect(
      shown({
        case: 'other',
        value: create(OtherCallSchema, {name: 'some_custom_tool', text: 'path: a.ts\nline: 3'}),
      }),
    ).toEqual({name: 'Some custom tool', asked: 'path: a.ts\nline: 3'});
    expect(
      shown({
        case: 'other',
        value: create(OtherCallSchema, {name: 'mcp__vibesys__post_handoff'}),
      }),
    ).toEqual({name: 'Post handoff', asked: null});
    expect(shown({case: 'shell', value: create(ShellCallSchema, {})}).asked).toBeNull();
  });

  it('a Claude and a Codex agent say, in their own words, what a message can do', () => {
    const {state} = stateOf(5, 4, true);
    const agents = Object.values(state.agents);
    const claude = agents.find(a => a.capabilities?.steers === true && a.role === AgentRole.WORKER);
    const codex = agents.find(a => a.capabilities?.steers === false && a.role === AgentRole.WORKER);
    if (claude === undefined || codex === undefined)
      throw new Error('the fixture mixes both tools');
    expect(page(state, claude)).toContain('reaches it mid-turn');
    expect(page(state, codex)).toContain('waits for its step');
  });

  it('shows Stop run only while the run can be stopped, and names its outcome', () => {
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
      expect(report(as(status), runId, now).includes('Stop run')).toBe(shown);
    }
    const command = create(CommandSchema, {id: 'c'});
    const sending = report(as(RunStatus.RUNNING), runId, now, {_tag: 'Sending', command});
    expect(sending).toContain('Stop run: sending');
    const refused = report(as(RunStatus.RUNNING), runId, now, {
      _tag: 'Refused',
      receipt: create(ReceiptSchema, {commandId: 'c', refusal: Refusal.RUN_NOT_RUNNING}),
    });
    expect(refused).toContain('Stop run refused \u00b7 Not running');
    const unsure = report(as(RunStatus.RUNNING), runId, now, {
      _tag: 'Unconfirmed',
      command,
      fault: {kind: 'DeadlineExceeded', detail: 'waited 15s', retryable: true},
    });
    expect(unsure).toContain('not confirmed');
    expect(unsure).toContain('Try again');
    expect(unsure).toContain('waited 15s');
  });

  it('the lead and a subagent on another tool are told apart by their chips', () => {
    expect(harnessWord(Harness.CLAUDE_CODE)).toBe('Claude Code');
    expect(harnessWord(Harness.CODEX)).toBe('Codex');
  });
});
