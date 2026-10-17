/**
 * The Agents face in a browser DOM, through its public props over a run read from span events: the
 * tree with its header, fan-in badge and folds; the compact list beside an opened agent; and the
 * agent pane with its facts and turns. Every move is the zoom event the view sends.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {initialCoreState} from '@vibesys/run-state';
import {EMPTY_TRACE} from '@vibesys/run-state/trace';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {FOLD_FROM} from '../../../../state/agent-tree.js';
import {traceLog} from '../../../../state/live.js';
import {createRunReader, type RunModel} from '../../../../state/run-model.js';
import type {ZoomEvent} from '../../../../state/zoom.js';
import type {ViewBase} from '../props.js';
import {AgentPane, AgentsView} from './agents-view.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));

beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});

afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

interface TurnOf {
  readonly agent: string;
  readonly at: number;
  readonly role?: string;
  readonly startedBy?: readonly string[];
  readonly status?: Span['status'];
  readonly title?: string;
  readonly within?: string;
}

/** The span events of a run whose agents take the turns described, in the order given. */
function eventsOf(turns: readonly TurnOf[]): RunEvent[] {
  const spans: Span[] = [];
  for (const turn of turns) {
    const status = turn.status ?? 'done';
    let parent: string | null = null;
    if (turn.within !== undefined) {
      parent = `ws:${turn.agent}:${turn.at}`;
      spans.push({
        id: parent,
        parent: null,
        type: 'flow',
        name: 'workstream',
        status: 'running',
        started_at: turn.at,
        label: {kind: 'workstream', title: turn.within, attrs_json: '{}'},
      } as Span);
    }
    spans.push({
      id: `turn:${turn.agent}:${turn.at}`,
      parent,
      type: 'operation',
      name: 'agent.turn',
      status,
      started_at: turn.at,
      ended_at: status === 'running' ? null : turn.at + 60,
      label: {
        kind: 'agent_turn',
        title: turn.title ?? turn.role ?? turn.agent,
        attrs_json: JSON.stringify({
          agent: turn.agent,
          role: turn.role ?? turn.agent,
          model: 'claude-sonnet-5',
          started_by: turn.startedBy ?? [],
        }),
      },
    } as Span);
  }
  return spans.map(
    (span, index) =>
      ({
        protocol_version: 1,
        run_id: 'run',
        sequence: index + 1,
        timestamp: '2026-01-01T00:00:00Z',
        type: 'span_started',
        data: {kind: 'span_started', span},
      }) as RunEvent,
  );
}

function modelOf(turns: readonly TurnOf[]): RunModel {
  return createRunReader().read(traceLog(EMPTY_TRACE, null, eventsOf(turns)), null);
}

function base(model: RunModel, sent: ZoomEvent[]): ViewBase {
  return {model, clock: {start: 0, now: 500}, zoom: event => void sent.push(event)};
}

async function mount(node: React.ReactNode): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () => createRoot(container).render(node));
  return container;
}

async function click(element: Element): Promise<void> {
  await act(async () => (element as HTMLElement).click());
}

const options = (container: HTMLElement): HTMLElement[] => [
  ...container.querySelectorAll<HTMLElement>('[role="option"]'),
];

function option(container: HTMLElement, text: string): HTMLElement {
  const found = options(container).find(each => each.textContent?.includes(text));
  if (found === undefined) throw new Error(`no option with ${text}`);
  return found;
}

const FAN_IN: readonly TurnOf[] = [
  {agent: 'boss', at: 1, role: 'orchestrator', status: 'running'},
  {agent: 'impl', at: 2, role: 'implementer', startedBy: ['boss'], within: 'ring padding'},
  {
    agent: 'judge',
    at: 3,
    role: 'judge',
    startedBy: ['boss', 'impl'],
    title: 'Padding keeps its gain',
    within: 'ring padding',
  },
];

describe('AgentsView', () => {
  test('draws each agent once in tree order with its header, activity and fan-in badge', async () => {
    const sent: ZoomEvent[] = [];
    const view = await mount(
      <AgentsView {...base(modelOf(FAN_IN), sent)} selected={null} compact={false} />,
    );
    expect(view.textContent).toContain('agent · started by the row above · now');
    expect(options(view).map(each => each.textContent)).toEqual([
      expect.stringMatching(/^orchestrator/),
      expect.stringMatching(/implementer.*ring padding/),
      expect.stringMatching(
        /judge.*ring padding · Padding keeps its gain.*\+ also under implementer/,
      ),
    ]);
  });

  test('selecting an agent opens it', async () => {
    const sent: ZoomEvent[] = [];
    const view = await mount(
      <AgentsView {...base(modelOf(FAN_IN), sent)} selected={null} compact={false} />,
    );
    await click(option(view, 'judge'));
    expect(sent).toEqual([{type: 'open-agent', agent: 'judge'}]);
  });

  test('beside an opened agent the list is compact and marks the opened agent', async () => {
    const view = await mount(<AgentsView {...base(modelOf(FAN_IN), [])} selected="impl" compact />);
    expect(view.textContent).not.toContain('started by the row above');
    expect(view.textContent).not.toContain('ring padding');
    expect(option(view, 'judge').textContent).toContain('+1 parent');
    expect(option(view, 'implementer').getAttribute('aria-selected')).toBe('true');
  });

  test('finished subtrees fold, active agents stay, and a press shows the fold in place', async () => {
    const finished: TurnOf[] = Array.from({length: FOLD_FROM}, (_, n) => ({
      agent: `old-${n}`,
      at: 2 + n,
      role: `old${n}`,
      startedBy: ['boss'],
    }));
    const model = modelOf([
      {agent: 'boss', at: 1, role: 'orchestrator', status: 'running'},
      ...finished,
      {agent: 'live', at: 20, role: 'live', startedBy: ['boss'], status: 'running'},
    ]);
    const view = await mount(<AgentsView {...base(model, [])} selected={null} compact={false} />);
    expect(options(view).map(each => each.textContent)).toEqual([
      expect.stringContaining('orchestrator'),
      expect.stringContaining('live'),
      expect.stringContaining(`+ ${FOLD_FROM} finished`),
    ]);
    await click(option(view, 'finished'));
    expect(options(view)).toHaveLength(2 + FOLD_FROM);
    expect(view.textContent).toContain('old0');
    expect(view.textContent).not.toContain('finished');
  });

  test('the opened agent stays in the compact list when its group was folded', async () => {
    const finished: TurnOf[] = Array.from({length: FOLD_FROM + 1}, (_, n) => ({
      agent: `old-${n}`,
      at: 2 + n,
      role: `old${n}`,
      startedBy: ['boss'],
    }));
    const model = modelOf([
      {agent: 'boss', at: 1, role: 'orchestrator', status: 'running'},
      ...finished,
    ]);
    const view = await mount(<AgentsView {...base(model, [])} selected="old-2" compact />);
    expect(option(view, 'old2').getAttribute('aria-selected')).toBe('true');
    expect(view.textContent).not.toContain('finished');
  });

  test('a large run draws only the rows near the visible part', async () => {
    const model = modelOf([
      {agent: 'boss', at: 1, role: 'orchestrator', status: 'running'},
      ...Array.from({length: 300}, (_, n) => ({
        agent: `a${n}`,
        at: 2 + n,
        role: `role${n}`,
        startedBy: ['boss'],
        status: 'running' as const,
      })),
    ]);
    const view = await mount(<AgentsView {...base(model, [])} selected={null} compact={false} />);
    expect(options(view).length).toBeGreaterThan(0);
    expect(options(view).length).toBeLessThan(100);
    expect(options(view)[0]?.getAttribute('aria-setsize')).toBe('301');
  });

  test('says so when the run has no agents', async () => {
    const view = await mount(<AgentsView {...base(modelOf([]), [])} selected={null} compact />);
    expect(view.textContent).toContain('No agents yet');
  });
});

describe('AgentPane', () => {
  const MODEL = modelOf([
    {agent: 'boss', at: 1, role: 'orchestrator', status: 'running'},
    {agent: 'impl', at: 2, role: 'implementer', startedBy: ['boss'], title: 'Pad the ring'},
    {agent: 'impl', at: 100, role: 'implementer', startedBy: ['boss'], title: 'Measure it'},
  ]);

  function pane(sent: ZoomEvent[]) {
    const agent = MODEL.agents.agents.find(each => each.id === 'impl');
    if (agent === undefined) throw new Error('no agent');
    return <AgentPane {...base(MODEL, sent)} core={null} agent={agent} />;
  }

  test('shows the agent’s facts and its turns in order', async () => {
    const view = await mount(pane([]));
    expect(view.textContent).toContain('claude-sonnet-5');
    expect(view.textContent).toMatch(/started by\s*orchestrator/);
    expect(options(view).map(each => each.textContent)).toEqual([
      expect.stringMatching(/1.*Pad the ring.*1m/),
      expect.stringMatching(/2.*Measure it.*1m/),
    ]);
  });

  test('names the resolved model of a running execution when the turn names none', async () => {
    const agent = MODEL.agents.agents.find(each => each.id === 'impl');
    if (agent === undefined) throw new Error('no agent');
    const unnamed = {...agent, model: null};
    const core = {
      ...initialCoreState(),
      activeExecutions: {
        x: {
          executionId: 'x',
          agentKind: 'implementer',
          roundLabel: null,
          roundNumber: null,
          roundKey: null,
          stage: 's',
          attempt: null,
          assignment: '',
          startedAt: '',
          activity: {mode: 'thinking' as const, summary: ''},
          model: 'gpt-6-luna',
        },
      },
    };
    const named = await mount(<AgentPane {...base(MODEL, [])} core={core} agent={unnamed} />);
    expect(named.textContent).toContain('gpt-6-luna');
    const none = await mount(<AgentPane {...base(MODEL, [])} core={null} agent={unnamed} />);
    expect(none.textContent).toContain('the run’s default');
  });

  test('the starter is a button that opens that agent, and a turn opens its step', async () => {
    const sent: ZoomEvent[] = [];
    const view = await mount(pane(sent));
    const starter = [...view.querySelectorAll('button')].find(
      each => each.textContent === 'orchestrator',
    );
    if (starter === undefined) throw new Error('no starter button');
    await click(starter);
    await click(option(view, 'Measure it'));
    expect(sent).toEqual([
      {type: 'open-agent', agent: 'boss'},
      {type: 'open-step', step: 'turn:impl:100'},
    ]);
  });
});
