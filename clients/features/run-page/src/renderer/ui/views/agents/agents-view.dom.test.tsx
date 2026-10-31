/**
 * The Agents face in a browser DOM, through its public props over a run read from span events: the
 * tree with its header, fan-in badge and folds; the compact list beside an opened agent; and the
 * agent pane with its facts and turns. Every move is the zoom event the view sends.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {EMPTY_LEDGER, initialCoreState, type TokenLedger} from '@vibesys/run-state';
import {EMPTY_TRACE} from '@vibesys/run-state/trace';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {FOLD_FROM} from '../../../../state/agent-tree.js';
import {traceLog} from '../../../../state/live.js';
import {createRunReader, type RunModel} from '../../../../state/run-model.js';
import {ledgerOf} from '../../../../state/usage-fixtures.js';
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
  return {
    model,
    clock: {start: 0, now: 500},
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: event => void sent.push(event),
  };
}

/** The page's side of the list's folds: the groups it keeps unfolded, and where unfolds go. */
function folds(
  unfolded: ReadonlySet<string> = new Set(),
  sent: string[] = [],
): {readonly unfolded: ReadonlySet<string>; readonly unfold: (parent: string) => void} {
  return {unfolded, unfold: parent => void sent.push(parent)};
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
      <AgentsView {...base(modelOf(FAN_IN), sent)} {...folds()} selected={null} compact={false} />,
    );
    expect(view.textContent).toContain('agent · what it is doing');
    expect(options(view).map(each => each.textContent)).toEqual([
      expect.stringMatching(/^Planner/),
      expect.stringMatching(/Builder.*ring padding/),
      expect.stringMatching(
        /Reviewer.*ring padding · Padding keeps its gain.*also started by Builder/,
      ),
    ]);
  });

  test('selecting an agent opens it', async () => {
    const sent: ZoomEvent[] = [];
    const view = await mount(
      <AgentsView {...base(modelOf(FAN_IN), sent)} {...folds()} selected={null} compact={false} />,
    );
    await click(option(view, 'Reviewer'));
    expect(sent).toEqual([{type: 'open-agent', agent: 'judge'}]);
  });

  test('beside an opened agent the list is compact and marks the opened agent', async () => {
    const view = await mount(
      <AgentsView {...base(modelOf(FAN_IN), [])} {...folds()} selected="impl" compact />,
    );
    expect(view.textContent).not.toContain('what it is doing');
    expect(view.textContent).not.toContain('ring padding');
    expect(option(view, 'Reviewer').textContent).toContain('+1 parent');
    expect(option(view, 'Builder').getAttribute('aria-selected')).toBe('true');
  });

  test('finished subtrees fold, active agents stay, and a press asks the page to unfold', async () => {
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
    const unfolds: string[] = [];
    const view = await mount(
      <AgentsView
        {...base(model, [])}
        {...folds(new Set(), unfolds)}
        selected={null}
        compact={false}
      />,
    );
    expect(options(view).map(each => each.textContent)).toEqual([
      expect.stringContaining('Planner'),
      expect.stringContaining('Live'),
      expect.stringContaining(`+ ${FOLD_FROM} finished`),
    ]);
    await click(option(view, 'finished'));
    expect(unfolds).toEqual(['boss']);
  });

  test('a group the page keeps unfolded shows in every list, whichever agent is open', async () => {
    // Two parents, each with a folded group; the user unfolded the first, then opened an agent of
    // the second: the compact list beside it still shows the first group whole.
    const group = (parent: string, from: number): TurnOf[] =>
      Array.from({length: FOLD_FROM}, (_, n) => ({
        agent: `${parent}-${n}`,
        at: from + n,
        role: `${parent}${n}`,
        startedBy: [parent],
      }));
    const model = modelOf([
      {agent: 'boss', at: 1, role: 'orchestrator', status: 'running'},
      {agent: 'lead', at: 2, role: 'lead', startedBy: ['boss'], status: 'running'},
      ...group('boss', 10),
      ...group('lead', 30),
    ]);
    const kept = new Set(['boss']);
    for (const selected of [null, 'lead-1', 'lead']) {
      const view = await mount(
        <AgentsView
          {...base(model, [])}
          {...folds(kept)}
          selected={selected}
          compact={selected !== null}
        />,
      );
      for (let n = 0; n < FOLD_FROM; n += 1) expect(view.textContent).toContain(`Boss${n}`);
      if (selected !== 'lead-1') expect(view.textContent).toContain(`+ ${FOLD_FROM} finished`);
    }
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
    const view = await mount(
      <AgentsView {...base(model, [])} {...folds()} selected="old-2" compact />,
    );
    expect(option(view, 'Old2').getAttribute('aria-selected')).toBe('true');
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
    const view = await mount(
      <AgentsView {...base(model, [])} {...folds()} selected={null} compact={false} />,
    );
    expect(options(view).length).toBeGreaterThan(0);
    expect(options(view).length).toBeLessThan(100);
    expect(options(view)[0]?.getAttribute('aria-setsize')).toBe('301');
  });

  test('says so when the run has no agents', async () => {
    const view = await mount(
      <AgentsView {...base(modelOf([]), [])} {...folds()} selected={null} compact />,
    );
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
    expect(view.textContent).toMatch(/Started by\s*Planner/);
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
    expect(none.textContent).toContain('The run’s default');
  });

  test('the starter is a button that opens that agent, and a turn opens its step', async () => {
    const sent: ZoomEvent[] = [];
    const view = await mount(pane(sent));
    const starter = [...view.querySelectorAll('button')].find(
      each => each.textContent === 'Planner',
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

describe('token usage', () => {
  const MODEL = modelOf([
    {agent: 'boss', at: 1, role: 'orchestrator', status: 'running'},
    {agent: 'impl', at: 2, role: 'implementer', startedBy: ['boss'], title: 'Pad the ring'},
    {agent: 'impl', at: 100, role: 'implementer', startedBy: ['boss'], title: 'Measure it'},
  ]);
  const LEDGER = ledgerOf([
    {span: 'turn:impl:2', input: 30_000, cached: 24_000, output: 2_000, reasoning: 500},
    {span: 'turn:impl:100', input: 18_000, output: 1_000},
  ]);

  test('the list gives each agent an in and an out column, with the cached share beside the input', async () => {
    const view = await mount(
      <AgentsView
        {...base(MODEL, [])}
        tokens={LEDGER}
        {...folds()}
        selected={null}
        compact={false}
      />,
    );
    expect(view.textContent).toContain('in (cached)');
    expect(view.textContent).toContain('out');
    const row = option(view, 'Builder');
    const [input, output] = [...row.querySelectorAll('.vs-pickrow-measure')];
    expect(input?.textContent).toBe('48k 24k cached');
    expect(output?.textContent).toBe('3k');
    expect(input?.getAttribute('title')).toBe(
      'Input 48,000 (24,000 cached, 24,000 uncached)\nOutput 3,000 (500 reasoning)\nTotal 51,000',
    );
  });

  test('an agent whose turns are still running says running, one that reported nothing says unknown', async () => {
    const view = await mount(
      <AgentsView
        {...base(MODEL, [])}
        tokens={LEDGER}
        {...folds()}
        selected={null}
        compact={false}
      />,
    );
    const cells = options(view).map(each =>
      [...each.querySelectorAll('.vs-pickrow-measure')].map(cell => cell.textContent),
    );
    expect(cells).toEqual([
      ['running', ''],
      ['48k 24k cached', '3k'],
    ]);
  });

  test('a run that reports no tokens has no column at all', async () => {
    const view = await mount(
      <AgentsView {...base(MODEL, [])} {...folds()} selected={null} compact={false} />,
    );
    expect(view.querySelector('.vs-pickrow-measure')).toBeNull();
    expect(view.textContent).not.toContain('tokens');
  });

  test('beside an opened agent the compact list leaves the column out', async () => {
    const view = await mount(
      <AgentsView {...base(MODEL, [])} tokens={LEDGER} {...folds()} selected="impl" compact />,
    );
    expect(view.querySelector('.vs-pickrow-measure')).toBeNull();
  });

  test('the pane names the agent’s total and each turn’s own', async () => {
    const agent = MODEL.agents.agents.find(each => each.id === 'impl');
    if (agent === undefined) throw new Error('no agent');
    const view = await mount(
      <AgentPane {...base(MODEL, [])} tokens={LEDGER} core={null} agent={agent} />,
    );
    expect(view.textContent).toMatch(/Tokens\s*in 48k \(24k cached\) · out 3k/);
    expect(options(view).map(each => each.textContent)).toEqual([
      expect.stringMatching(/Pad the ring.*in 30k \(24k cached\) · out 2k/),
      expect.stringMatching(/Measure it.*in 18k \(cached unknown\) · out 1k/),
    ]);
  });

  test('a turn whose provider reported no counts says unknown, not a zero', async () => {
    const quiet = ledgerOf([{span: 'turn:impl:2'}]);
    const agent = MODEL.agents.agents.find(each => each.id === 'impl');
    if (agent === undefined) throw new Error('no agent');
    const view = await mount(
      <AgentPane {...base(MODEL, [])} tokens={quiet} core={null} agent={agent} />,
    );
    expect(view.textContent).not.toContain('tokens');
    expect(view.querySelector('.vs-pickrow-measure')).toBeNull();
  });

  test('the figures follow the ledger as turns finish', async () => {
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);
    const draw = (tokens: TokenLedger) =>
      act(async () =>
        root.render(
          <AgentsView
            {...base(MODEL, [])}
            tokens={tokens}
            {...folds()}
            selected={null}
            compact={false}
          />,
        ),
      );
    await draw(ledgerOf([{span: 'turn:impl:2', input: 1_000}]));
    expect(option(container, 'Builder').textContent).toContain('1k');
    await draw(
      ledgerOf([
        {span: 'turn:impl:2', input: 1_000},
        {span: 'turn:impl:100', input: 1_500},
      ]),
    );
    expect(option(container, 'Builder').textContent).toContain('2.5k');
  });
});

describe('who ran the agents', () => {
  const who = new Map([
    ['turn:boss:1', {harness: 'codex', model: 'gpt-6-luna', client: null}],
    ['turn:impl:2', {harness: 'claude', model: 'sonnet-5', client: null}],
  ]);

  test('each row says the harness and model that ran its turns, from the records', async () => {
    const view = await mount(
      <AgentsView
        {...base(modelOf(FAN_IN), [])}
        who={who}
        {...folds()}
        selected={null}
        compact={false}
      />,
    );
    expect(option(view, 'Planner').textContent).toContain('gpt-6-luna on codex');
    expect(option(view, 'Builder').textContent).toContain('sonnet-5 on claude');
    // The judge's turn has no record: its row names nothing it does not know.
    expect(option(view, 'Reviewer').textContent).not.toContain(' on ');
  });

  test('the agent pane names the harness and the model', async () => {
    const model = modelOf(FAN_IN);
    const agent = model.agents.agents.find(each => each.id === 'impl');
    if (agent === undefined) throw new Error('no implementer');
    const pane = await mount(
      <AgentPane {...base(model, [])} who={who} core={initialCoreState()} agent={agent} />,
    );
    expect(pane.textContent).toContain('Modelsonnet-5');
    expect(pane.textContent).toContain('Harnessclaude');
  });
});
