import {describe, expect, test} from 'bun:test';
import {generateScenario} from '@vibesys/orchestrator-fixtures';
import {type Agent, AgentRole, PlanStatus} from '@vibesys/orchestrator-proto';
import {applyEvent, emptyState, type OrchestratorState} from '@vibesys/orchestrator-state';
import fc from 'fast-check';
import {renderToStaticMarkup} from 'react-dom/server';
import {AgentPanel} from './agent-panel.js';
import {verdictOf} from './handoff-cards.js';
import type {RunView} from './route.js';
import {RunList} from './run-list.js';
import {RunScreen} from './run-screen.js';
import {planWord} from './words.js';

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

function panel(state: OrchestratorState, agent: Agent): string {
  return renderToStaticMarkup(
    <AgentPanel
      state={state}
      agent={agent}
      name="Worker 1"
      older="idle"
      olderError={null}
      box={{status: 'idle'}}
      showOlder={() => undefined}
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
          {host: 'host-a', connection: 'live', state: a.state},
          {host: 'host-b', connection: 'waiting', state: b.state},
        ]}
        open={() => undefined}
      />,
    );
    expect(html).toContain('host-a');
    expect(html).toContain('host-b');
    expect(html).toContain('Lost the connection to host-b');
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
