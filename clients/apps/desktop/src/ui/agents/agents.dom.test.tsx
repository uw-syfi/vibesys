/**
 * The agents screens by what a person does: unfold a count bar, open an agent from the tree, open
 * a folded thought and a call, and ask for the body a page left out.
 */
import {describe, expect, it} from '@effect/vitest';
import type {Agent} from '@vibesys/orchestrator-proto';
import {applyChange, emptyState, itemIs, type OrchestratorState} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {AgentPage} from './agent-page.js';
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
        trail={[]}
        notice={null}
        open={id => opened.push(id)}
      />,
    );

  it('is a page with a key to its bars: running, done, failed, a result returned, the machine, folded', () => {
    const screen = view();
    const key = screen.container.querySelector('ul[aria-label="Key to the bars"]');
    expect([...(key?.querySelectorAll('li') ?? [])].map(item => item.textContent)).toEqual([
      'Running',
      'Done',
      'Failed',
      'Result returned',
      'host-aMachine',
      'Folded',
    ]);
    expect(screen.container.querySelector('h1')?.textContent).toBe('Agents');
    screen.unmount();
  });

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

describe('an agent page', () => {
  const page = (agent: Agent) =>
    mount(
      <AgentPage
        state={state}
        nowMs={scenario.endMs}
        agent={agent}
        host="host-a"
        trail={[{id: 'runs', label: 'Runs'}]}
        notice={null}
        sendProgress={undefined}
        history={{
          earlier: {_tag: 'Idle'},
          wholes: {},
          loadEarlier: () => undefined,
          expand: () => undefined,
        }}
        send={() => undefined}
        retrySend={() => undefined}
        open={() => undefined}
      />,
    );

  it('reads as a centred column: where the agent sits and ran, its conversation, then the box to write in', () => {
    for (const agent of Object.values(state.agents).slice(0, 6)) {
      const screen = page(agent);
      const column = screen.container.querySelector('[data-width="reading"]');
      expect(column).not.toBeNull();
      // Placement, then the conversation, then the message box, all inside the one column.
      const sections = [...(column?.children ?? [])].map(each => each.textContent ?? '');
      const placement = sections.findIndex(text => text.startsWith('Placement'));
      const box = sections.findIndex(text => text.startsWith('Message'));
      expect(placement).toBeGreaterThanOrEqual(0);
      expect(box).toBeGreaterThan(placement);
      expect(column?.querySelector('.vs-conversation')).not.toBeNull();
      expect(column?.textContent).toContain('host-a');
      screen.unmount();
    }
  });

  it('names its place in the spawn tree only when it has one: the lead is at the top', () => {
    const lead = Object.values(state.agents).find(agent => agent.parentAgentId === '');
    const child = Object.values(state.agents).find(agent => agent.parentAgentId !== '');
    if (lead === undefined || child === undefined) throw new Error('the run has a lead and agents');
    const place = (screen: ReturnType<typeof mount>) =>
      screen.container.querySelector('nav[aria-label="Place in the spawn tree"]');
    const top = page(lead);
    expect(place(top)).toBeNull();
    expect(top.container.querySelector('h1')?.textContent).toBe('lead');
    top.unmount();
    const below = page(child);
    expect(place(below)?.querySelectorAll('li').length).toBeGreaterThan(1);
    below.unmount();
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
