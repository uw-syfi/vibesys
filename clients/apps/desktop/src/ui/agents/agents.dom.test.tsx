/**
 * The agents screens by what a person does: unfold a count bar, open an agent from the tree, open
 * a folded thought and a call, and ask for the body a page left out.
 */
import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {type Agent, AgentStatus, CommandSchema} from '@vibesys/orchestrator-proto';
import {
  applyChange,
  type CommandProgress,
  emptyState,
  itemIs,
  type OrchestratorState,
} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {AgentPage} from './agent-page.js';
import {AgentTranscript, type HistoryAccess} from './agent-transcript.js';
import {AgentsScreen} from './agents-screen.js';

installDom();

const scenario = generateScenario({seed: 5, subagents: 20, live: false, tree: true, depth: 2});
const state: OrchestratorState = scenario.changes.reduce(applyChange, emptyState);
const MESSAGEABLE = [
  AgentStatus.UNSPECIFIED,
  AgentStatus.LAUNCHING,
  AgentStatus.ACTIVE,
  AgentStatus.WAITING,
  AgentStatus.IDLE,
];
const OVER = [AgentStatus.ENDED, AgentStatus.FAILED, AgentStatus.LOST];
const SENDING: CommandProgress = {
  _tag: 'Sending',
  command: create(CommandSchema, {id: 'sending-message'}),
};

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
        reading={null}
        open={id => opened.push(id)}
      />,
    );

  it('draws its time axis at round, equally spaced moments, whatever moment the run starts at', () => {
    const screen = view();
    const marks = [...screen.container.querySelectorAll('.vs-gantt-tick')].map(
      tick => tick.textContent ?? '',
    );
    const seconds = marks.map(text => {
      const [clock = '', half = ''] = text.split(' ');
      const [h = 0, m = 0, sec = 0] = clock.split(':').map(Number);
      return ((h % 12) + (half === 'PM' ? 12 : 0)) * 3600 + m * 60 + sec;
    });
    expect(seconds.length).toBeGreaterThanOrEqual(2);
    const step = (seconds[1] ?? 0) - (seconds[0] ?? 0);
    expect(step).toBeGreaterThan(0);
    seconds.forEach((at, index) => {
      expect(at).toBe((seconds[0] ?? 0) + index * step);
      expect(at % step).toBe(0);
    });
    screen.unmount();
  });

  it('is a page with a key to its bars: running, done, failed, a result returned, the machine, folded', () => {
    const screen = view();
    const key = screen.container.querySelector('ul[aria-label="Key to the bars"]');
    expect([...(key?.querySelectorAll('li') ?? [])].map(item => item.textContent)).toEqual([
      'Running',
      'Done',
      'Failed',
      'Result returned',
      ' Machine',
      'Folded',
    ]);
    expect(screen.container.querySelector('h1')?.textContent).toBe('Agents');
    screen.unmount();
  });

  it('shows the big group of workers as one folded row, and a press on it unfolds the agents', () => {
    const screen = view();
    const folded = labels(screen.container).find(label => label.startsWith('Worker ×'));
    expect(folded).toMatch(/folded$/);
    const before = options(screen.container).length;
    const row = options(screen.container).find(one => one.getAttribute('aria-label') === folded);
    if (row === undefined) throw new Error('the group row is drawn');
    screen.press(row);
    expect(labels(screen.container).find(label => label.startsWith('Worker ×'))).toMatch(/open$/);
    expect(options(screen.container).length).toBeGreaterThan(before);
    screen.press(
      options(screen.container).find(one =>
        one.getAttribute('aria-label')?.startsWith('Worker ×'),
      ) ?? row,
    );
    expect(options(screen.container)).toHaveLength(before);
    screen.unmount();
  });

  it('opens an agent by a press on its row, and a press on a chevron folds without opening', () => {
    opened.length = 0;
    const screen = view();
    const planner = options(screen.container).find(one =>
      one.getAttribute('aria-label')?.startsWith('Planner 2'),
    );
    if (planner === undefined) throw new Error('Planner 2 is drawn');
    screen.press(planner);
    expect(opened).toHaveLength(1);
    const chevron = planner.querySelector<HTMLElement>('.vs-gantt-fold');
    if (chevron === null) throw new Error('Planner 2 holds agents');
    expect(planner.getAttribute('aria-label')).toMatch(/open$/);
    screen.press(chevron);
    expect(opened).toHaveLength(1);
    expect(planner.getAttribute('aria-label')).toMatch(/folded$/);
    screen.unmount();
  });
});

describe('an agent page', () => {
  const page = (agent: Agent, sendProgress: CommandProgress | undefined = undefined) =>
    mount(
      <AgentPage
        state={state}
        nowMs={scenario.endMs}
        agent={agent}
        host="host-a"
        trail={[{id: 'runs', label: 'Runs'}]}
        reading={null}
        sendProgress={sendProgress}
        stopProgress={undefined}
        history={{
          earlier: {_tag: 'Idle'},
          wholes: {},
          loadEarlier: () => undefined,
          expand: () => undefined,
        }}
        send={() => undefined}
        retrySend={() => undefined}
        stop={() => undefined}
        retryStop={() => undefined}
        open={() => undefined}
      />,
    );

  it('reads in one scroll: where the agent sits and ran, its conversation filling the page, then the box to write in', () => {
    for (const agent of Object.values(state.agents).filter(one => !OVER.includes(one.status))) {
      const screen = page(agent);
      // The page's body does not scroll; the conversation takes what the cards leave and scrolls itself.
      const body = screen.container.querySelector('.vs-page-scroll[data-fill] > .vs-page-body');
      expect(body).not.toBeNull();
      const sections = [...(body?.children ?? [])];
      const text = sections.map(each => each.textContent ?? '');
      const placement = text.findIndex(each => each.startsWith('Placement'));
      const box = text.findIndex(each => each.startsWith('Message'));
      const conversation = sections.findIndex(each =>
        each.matches('.vs-conversation[data-size="fill"]'),
      );
      expect(placement).toBeGreaterThanOrEqual(0);
      expect(conversation).toBeGreaterThan(placement);
      expect(box).toBeGreaterThan(conversation);
      // One conversation, drawn as a direct child of the body: no column of its own around it.
      expect(screen.container.querySelectorAll('.vs-conversation')).toHaveLength(1);
      expect(body?.textContent).toContain('host-a');
      screen.unmount();
    }
  });

  it('offers a message only while the host can accept one, across every agent status', () => {
    const agent = Object.values(state.agents)[0];
    if (agent === undefined) throw new Error('the run has an agent');
    for (const status of [...MESSAGEABLE, ...OVER]) {
      const screen = page({...agent, status});
      const expected = MESSAGEABLE.includes(status) ? 1 : 0;
      expect(screen.controls('Send message')).toHaveLength(expected);
      if (expected === 1)
        expect(screen.control('Send message').textContent?.trim()).toBe('Send message');
      expect(screen.text().includes('Message Lead')).toBe(expected === 1);
      screen.unmount();
    }
  });

  it('keeps the message object in the action and its in-flight outcome', () => {
    const agent = Object.values(state.agents).find(one => MESSAGEABLE.includes(one.status));
    if (agent === undefined) throw new Error('the run has a messageable agent');
    const screen = page(agent, SENDING);
    expect(screen.control('Send message').textContent?.trim()).toBe('Sending message');
    expect(screen.text()).toContain('Send message: sending');
    screen.unmount();
  });

  it('says in its header that older turns are not shown with the count the host gave, and only when it left some out', () => {
    const agent = Object.values(state.agents)[0];
    if (agent === undefined) throw new Error('the run has agents');
    const read = (turnsLeftOut: number) => ({
      ...state,
      histories: {
        ...state.histories,
        [agent.runId]: {
          sequence: 1n,
          complete: true,
          turnsLeftOut,
          checkInsLeftOut: 0,
          agentsLeftOut: 0,
        },
      },
    });
    const shown = (held: OrchestratorState) => {
      const screen = mount(
        <AgentPage
          state={held}
          nowMs={scenario.endMs}
          agent={agent}
          host="host-a"
          trail={[{id: 'runs', label: 'Runs'}]}
          reading={null}
          sendProgress={undefined}
          stopProgress={undefined}
          history={{
            earlier: {_tag: 'Idle'},
            wholes: {},
            loadEarlier: () => undefined,
            expand: () => undefined,
          }}
          send={() => undefined}
          retrySend={() => undefined}
          stop={() => undefined}
          retryStop={() => undefined}
          open={() => undefined}
        />,
      );
      const text = screen.container.querySelector('.vs-page-meta')?.textContent ?? '';
      screen.unmount();
      return text;
    };
    expect(shown(read(12))).toContain('12 turns not shown');
    expect(shown(read(0))).not.toContain('not shown');
    expect(shown(state)).not.toContain('not shown');
  });

  it('counts its turns in the singular for one', () => {
    for (const agent of Object.values(state.agents)) {
      const screen = page(agent);
      const turns = Object.values(state.turns).filter(turn => turn.agentId === agent.id).length;
      expect(screen.container.textContent).not.toContain('1 turns');
      expect(screen.container.textContent).toContain(`${turns} ${turns === 1 ? 'turn' : 'turns'}`);
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
    expect(top.container.querySelector('h1')?.textContent).toBe('Lead');
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
