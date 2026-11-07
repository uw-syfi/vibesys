/**
 * The agents screens by what a person does: unfold a count bar, open an agent from the tree, open
 * a folded thought and a call, and ask for the body a page left out.
 */
import {describe, expect, it} from '@effect/vitest';
import {applyChange, emptyState, itemIs, type OrchestratorState} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {AgentTranscript, type HistoryAccess} from './agent-transcript.js';
import {AgentsScreen} from './agents-screen.js';

installDom();

const scenario = generateScenario({seed: 5, subagents: 20, live: false, tree: true, depth: 2});
const state: OrchestratorState = scenario.changes.reduce(applyChange, emptyState);

const options = (container: HTMLElement): HTMLElement[] => [
  ...container.querySelectorAll<HTMLElement>('[role="option"]'),
];
const labels = (container: HTMLElement): string[] =>
  options(container).map(row => row.getAttribute('aria-label') ?? '');

describe('the agents screen', () => {
  const opened: string[] = [];
  const view = () =>
    mount(
      <AgentsScreen
        state={state}
        runId={scenario.runId}
        host="host-a"
        nowMs={scenario.endMs}
        selected={null}
        open={id => opened.push(id)}
      />,
    );

  it('shows the big group of workers as one folded row, and a press on it unfolds the agents', () => {
    const screen = view();
    const folded = labels(screen.container).find(label => label.startsWith('worker ×'));
    expect(folded).toMatch(/folded$/);
    const before = options(screen.container).length;
    const row = options(screen.container).find(one => one.getAttribute('aria-label') === folded);
    if (row === undefined) throw new Error('the group row is drawn');
    screen.press(row);
    expect(labels(screen.container).find(label => label.startsWith('worker ×'))).toMatch(/open$/);
    expect(options(screen.container).length).toBeGreaterThan(before);
    screen.press(
      options(screen.container).find(one =>
        one.getAttribute('aria-label')?.startsWith('worker ×'),
      ) ?? row,
    );
    expect(options(screen.container)).toHaveLength(before);
    screen.unmount();
  });

  it('opens an agent by a press on its row, and a press on a chevron folds without opening', () => {
    opened.length = 0;
    const screen = view();
    const planner = options(screen.container).find(one =>
      one.getAttribute('aria-label')?.startsWith('planner-2'),
    );
    if (planner === undefined) throw new Error('planner-2 is drawn');
    screen.press(planner);
    expect(opened).toHaveLength(1);
    const chevron = planner.querySelector<HTMLElement>('.vs-gantt-fold');
    if (chevron === null) throw new Error('planner-2 holds agents');
    expect(planner.getAttribute('aria-label')).toMatch(/open$/);
    screen.press(chevron);
    expect(opened).toHaveLength(1);
    expect(planner.getAttribute('aria-label')).toMatch(/folded$/);
    screen.unmount();
  });
});

describe('an agent transcript', () => {
  const worker = Object.values(state.agents).find(agent => agent.id.includes('worker')) ?? null;
  const id = worker?.id ?? '';
  const target = Object.values(state.items).find(
    item =>
      item.agentId === id && itemIs(item, 'tool') && item.body.value.output.includes('variant'),
  );
  const elided: OrchestratorState = {...state, elided: {[target?.id ?? '']: 12_000}};

  function transcript(
    history: Partial<HistoryAccess>,
    shown: OrchestratorState,
    agentId: string = id,
  ) {
    return mount(
      <AgentTranscript
        state={shown}
        agentId={agentId}
        host="host-a"
        nowMs={scenario.endMs}
        history={{
          earlier: {_tag: 'Idle'},
          wholes: {},
          loadEarlier: () => undefined,
          expand: () => undefined,
          ...history,
        }}
        open={() => undefined}
      />,
    );
  }

  it('opens a folded thought on a press and folds it again', () => {
    const lead = Object.values(state.agents).find(agent => agent.parentAgentId === '');
    const screen = transcript({}, state, lead?.id ?? '');
    const heads = [...screen.container.querySelectorAll<HTMLElement>('.vs-thought button')];
    const head = heads[0];
    if (head === undefined) throw new Error('the agent thought');
    expect(head.getAttribute('aria-expanded')).toBe('false');
    screen.press(head);
    expect(head.getAttribute('aria-expanded')).toBe('true');
    screen.press(head);
    expect(head.getAttribute('aria-expanded')).toBe('false');
    screen.unmount();
  });

  it('asks for the whole body only when a row whose body a page left out is opened', () => {
    const asked: number[] = [];
    const screen = transcript({expand: ordinal => asked.push(ordinal)}, elided);
    const heads = [...screen.container.querySelectorAll<HTMLElement>('.vs-tool-call button')];
    expect(asked).toEqual([]);
    for (const head of heads) screen.press(head);
    expect(asked).toEqual([target?.ordinal]);
    expect(screen.text()).toContain('12 KB not loaded');
    screen.unmount();
  });
});
