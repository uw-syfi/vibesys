/** The step view through its props, with a run built from span and transcript events. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {EMPTY_LEDGER, initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import type {CandidateStep} from '@vibesys/run-state/candidates';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {createRunReader, runPageInitial, runPageStep} from '../../../../state/index.js';
import {ledgerOf} from '../../../../state/usage-fixtures.js';
import type {ZoomEvent} from '../../../../state/zoom.js';
import type {ViewBase} from '../props.js';
import {ItemView, itemHeader, StepView, type StepViewProps, stepHeader} from './step-view.js';

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

function span(id: string, parent: string | null, patch: Partial<Span>): Span {
  return {
    id,
    parent,
    type: parent === null ? 'flow' : 'operation',
    name: id,
    status: 'done',
    started_at: 1,
    ended_at: 101,
    ...patch,
  } as Span;
}

function labelled(
  id: string,
  parent: string,
  kind: string,
  title: string,
  extra: Partial<Span> = {},
  attrs: Record<string, unknown> = {},
): Span {
  return span(id, parent, {
    label: {kind, title, attrs_json: JSON.stringify(attrs), links: []},
    ...extra,
  } as Partial<Span>);
}

const SPANS: readonly Span[] = [
  span('flow:root#1', null, {name: 'dynamic'}),
  labelled('ws', 'flow:root#1', 'workstream', 'Pad the counters', {}, {hypothesis_id: 'h1'}),
  labelled('turn', 'ws', 'agent_turn', 'implementer', {}, {role: 'implementer'}),
  labelled(
    'gate',
    'ws',
    'gate',
    'benchmark',
    {},
    {
      stage: 'benchmark',
      metric_value: 13318502.55,
      vs_baseline: 5.72,
      failed: {check: 'accuracy', completed: 93, required: 100, unit: 'trials'},
    },
  ),
];

function chunk(sequence: number, channel: 'assistant' | 'analysis', content: string): RunEvent {
  return event(sequence, 'agent_output_chunk', {
    invocation_id: 'inv',
    agent_kind: 'implementer',
    span_id: 'turn',
    data: {kind: 'agent_output_chunk', channel, content},
  });
}

function call(sequence: number, id: string, command: string): RunEvent {
  return event(sequence, 'tool_call', {
    invocation_id: 'inv',
    agent_kind: 'implementer',
    span_id: 'turn',
    data: {kind: 'tool_call', tool: 'Bash', call_id: id, args: {command}},
  });
}

function result(sequence: number, id: string, content: string, error = false): RunEvent {
  return event(sequence, 'tool_result', {
    invocation_id: 'inv',
    agent_kind: 'implementer',
    span_id: 'turn',
    data: {kind: 'tool_result', tool: 'Bash', call_id: id, content, is_error: error},
  });
}

interface Fixture {
  readonly base: ViewBase;
  readonly core: StepViewProps['core'];
  readonly turn: CandidateStep;
  readonly gate: CandidateStep;
  readonly zooms: ZoomEvent[];
}

/** The run after `transcript`, as a view receives it. */
function fixture(
  transcript: readonly RunEvent[],
  turnStatus: Span['status'] = 'done',
  more: readonly Span[] = [],
): Fixture {
  let state = runPageInitial({
    identity: {host: 'ssh:host-b', runId: '20261015-163047'},
    title: {project: '~/p', task: 'mpmc'},
    hostLabel: 'host-b',
    unavailable: {},
  });
  state = {...state, link: {kind: 'attached', instance: 'a1b2c3d4e5f6'}};
  state = runPageStep(state, {
    type: 'run-data',
    wallMs: 0,
    data: {
      kind: 'events',
      events: [...SPANS, ...more]
        .map(each => (each.id === 'turn' ? {...each, status: turnStatus} : each))
        .map((each, index) => ({
          ...event(index + 1, 'span_started'),
          data: {kind: 'span_started', span: each},
        })) as RunEvent[],
    },
  }).state;
  const core = reduceEventBatch(initialCoreState(), transcript);
  state = runPageStep(state, {type: 'run-core', core}).state;
  const model = createRunReader().read(state.run.trace, null);
  const steps = model.candidates.candidates[0]?.attempts[0]?.steps ?? [];
  const turn = steps.find(step => step.kind === 'agent_turn');
  const gate = steps.find(step => step.kind === 'gate');
  if (turn === undefined || gate === undefined) throw new Error('the fixture has no steps');
  const zooms: ZoomEvent[] = [];
  return {
    base: {
      model,
      clock: {start: 0, now: 101},
      tokens: EMPTY_LEDGER,
      who: new Map(),
      zoom: each => zooms.push(each),
    },
    core: state.run.core,
    turn,
    gate,
    zooms,
  };
}

function viewProps(fx: Fixture, step: CandidateStep, item: string | null): StepViewProps {
  return {
    ...fx.base,
    core: fx.core,
    idea: fx.base.model.candidates.candidates[0] ?? null,
    agent: null,
    step,
    item,
    compact: false,
  };
}

async function mount(node: ReactNode): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(node));
  return container;
}

async function click(element: Element): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
  });
}

function all(container: HTMLElement, selector: string): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(selector)];
}

function one(container: HTMLElement, selector: string): HTMLElement {
  const found = container.querySelector<HTMLElement>(selector);
  if (found === null) throw new Error(`no ${selector}`);
  return found;
}

const TURN: readonly RunEvent[] = [
  chunk(10, 'analysis', 'Counters share a cache line.'),
  call(11, 'c1', 'read src/lib.rs'),
  result(12, 'c1', 'fn main() {}'),
  call(13, 'c2', 'cargo test'),
  result(14, 'c2', '18 passed'),
  chunk(15, 'assistant', 'Padding both counters.'),
  call(16, 'c3', 'cargo bench'),
  result(17, 'c3', 'exit 2', true),
];

describe('an agent turn', () => {
  test('reads thinking and text in order, with consecutive tool calls folded into one line', async () => {
    const fx = fixture(TURN);
    const container = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    const blocks = all(container, '.vs-thought, .vs-said, .vs-toolgroup').map(
      block => block.className.split(' ')[0],
    );
    expect(blocks).toEqual(['vs-thought', 'vs-toolgroup', 'vs-said', 'vs-toolgroup']);
    const heads = all(container, '.vs-toolgroup-head');
    expect(heads[0]?.textContent).toContain('2 tools');
    expect(heads[1]?.textContent).toContain('1 tool');
  });

  test('a failed call stays a red line while its group is closed, and the group opens in place', async () => {
    const fx = fixture(TURN);
    const container = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    expect(all(container, '.vs-toolgroup-row').map(row => row.getAttribute('data-state'))).toEqual([
      'failed',
    ]);
    const first = one(container, '.vs-toolgroup-head');
    await click(first);
    expect(first.getAttribute('aria-expanded')).toBe('true');
    expect(all(container, '.vs-toolgroup-row')).toHaveLength(3);
    expect(fx.zooms).toEqual([]);
  });

  test('pressing a tool line opens that item', async () => {
    const fx = fixture(TURN);
    const container = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    await click(one(container, '.vs-toolgroup-row'));
    expect(fx.zooms).toHaveLength(1);
    expect(fx.zooms[0]).toMatchObject({type: 'open-item'});
  });

  test('beside an opened item its group is open and its row is marked', async () => {
    const fx = fixture(TURN);
    const opened = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    await click(one(opened, '.vs-toolgroup-head'));
    await click(all(opened, '.vs-toolgroup-row')[1] as HTMLElement);
    const item = (fx.zooms[0] as {item: string}).item;
    const container = await mount(<StepView {...viewProps(fx, fx.turn, item)} compact />);
    const marked = all(container, '[aria-current="true"]');
    expect(marked).toHaveLength(1);
    expect(marked[0]?.textContent).toContain('cargo test');
    expect(all(container, '.vs-toolgroup-head')[0]?.getAttribute('aria-expanded')).toBe('true');
  });

  test('says when nothing is recorded, and waits while the run is not read yet', async () => {
    const fx = fixture([]);
    const empty = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    expect(empty.textContent).toContain('Nothing recorded for this step');
    const unread = await mount(<StepView {...viewProps(fx, fx.turn, null)} core={null} />);
    expect(unread.textContent).toContain('Reading the turn');
  });

  test('a turn of 300 entries draws only the blocks near the visible part', async () => {
    const entries: RunEvent[] = [];
    for (let index = 0; index < 150; index++) {
      entries.push(chunk(10 + index * 2, 'assistant', `said ${index}`));
      entries.push(chunk(11 + index * 2, 'analysis', `thought ${index}`));
    }
    const fx = fixture(entries);
    const container = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    const drawn = all(container, '.vs-said, .vs-thought').length;
    expect(drawn).toBeGreaterThan(0);
    expect(drawn).toBeLessThan(150);
  });

  test('its header carries the turn’s duration, tools and failures', () => {
    const fx = fixture(TURN);
    const header = stepHeader(viewProps(fx, fx.turn, null));
    expect(header.kicker).toBe('Agent turn');
    expect(header.meta).toBeDefined();
  });

  test('its header adds the turn’s tokens beside duration and tools, exact on hover', async () => {
    const fx = fixture(TURN);
    const tokens = ledgerOf([
      {span: fx.turn.span, input: 40_000, cached: 30_000, output: 8_000, reasoning: 3_000},
    ]);
    const header = stepHeader({...viewProps(fx, fx.turn, null), tokens});
    const meta = await mount(header.meta);
    expect(meta.textContent).toMatch(/ · in 40k \(30k cached\) · out 8k$/);
    expect(meta.querySelector('[title]')?.getAttribute('title')).toBe(
      'Input 40,000 (30,000 cached, 10,000 uncached)\nOutput 8,000 (3,000 reasoning)\nTotal 48,000',
    );
  });

  test('a turn with no reported tokens keeps the header as it was', async () => {
    const fx = fixture(TURN);
    const header = stepHeader(viewProps(fx, fx.turn, null));
    const meta = await mount(header.meta);
    expect(meta.textContent).not.toMatch(/\bin \d|\bout \d|unknown/);
    expect(meta.querySelector('[title]')).toBeNull();
  });
});

describe('a gate', () => {
  const LOG = [
    event(10, 'subprocess_output', {
      span_id: 'gate',
      data: {
        kind: 'subprocess_output',
        process_id: 'p1',
        process_kind: 'check',
        stream: 'stdout',
        content: 'accuracy: 93/100',
      },
    }),
  ];

  test('shows the verdict first, then its numbers, then its log', async () => {
    const fx = fixture(LOG);
    const container = await mount(<StepView {...viewProps(fx, fx.gate, null)} />);
    const text = container.textContent ?? '';
    expect(text.indexOf('Failed')).toBeLessThan(text.indexOf('13,318,503'));
    expect(text).toContain('5.72×');
    expect(text).toContain('Accuracy · 93/100 trials');
    expect(one(container, '[role="log"]').textContent).toContain('accuracy: 93/100');
    expect(stepHeader(viewProps(fx, fx.gate, null))).toEqual({});
  });

  test('says when it has no log', async () => {
    const fx = fixture([]);
    const container = await mount(<StepView {...viewProps(fx, fx.gate, null)} />);
    expect(container.textContent).toContain('No log recorded for this gate');
  });
});

describe('an item', () => {
  async function itemOf(fx: Fixture, name: RegExp): Promise<string> {
    const opened = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    for (const head of all(opened, '.vs-toolgroup-head')) await click(head);
    const row = all(opened, '.vs-toolgroup-row').find(each => name.test(each.textContent ?? ''));
    if (row === undefined) throw new Error('no such row');
    await click(row);
    return (fx.zooms.at(-1) as {item: string}).item;
  }

  test('shows its outcome, input and result in full', async () => {
    const fx = fixture(TURN);
    const item = await itemOf(fx, /cargo test/);
    const container = await mount(
      <ItemView {...fx.base} core={fx.core} step={fx.turn} item={item} />,
    );
    expect(container.textContent).toContain('Done');
    const log = one(container, '[role="log"]').textContent ?? '';
    expect(log.indexOf('Input')).toBeLessThan(log.indexOf('Result'));
    expect(log).toContain('cargo test');
    expect(log).toContain('18 passed');
  });

  test('a failed call reads as an error, and names itself in its header', async () => {
    const fx = fixture(TURN);
    const item = await itemOf(fx, /cargo bench/);
    const container = await mount(
      <ItemView {...fx.base} core={fx.core} step={fx.turn} item={item} />,
    );
    expect(container.textContent).toContain('Failed');
    expect(one(container, '[data-stream="err"]').textContent).toContain('exit 2');
    expect(itemHeader({...fx.base, core: fx.core, step: fx.turn, item})).toEqual({
      kicker: 'tool call · Bash',
      title: 'cargo bench',
    });
  });

  test('an item that is not in the step says so', async () => {
    const fx = fixture(TURN);
    const container = await mount(
      <ItemView {...fx.base} core={fx.core} step={fx.turn} item="nope" />,
    );
    expect(container.textContent).toContain('This item is not in the step');
  });
});

function reply(sequence: number, summary = 'Padded the counters.'): RunEvent {
  return event(sequence, 'agent_execution_finished', {
    invocation_id: 'inv',
    agent_kind: 'implementer',
    span_id: 'turn',
    data: {
      kind: 'agent_execution_finished',
      result: '{}',
      reply: {
        kind: 'ImplementerReply',
        json_schema: {
          properties: {
            summary: {type: 'string'},
            changed: {type: 'array', items: {type: 'string'}},
            outcome: {enum: ['done', 'blocked'], type: 'string'},
          },
          required: ['summary'],
          type: 'object',
        },
        value: {summary, changed: ['src/lib.rs'], outcome: 'done'},
      },
    },
  } as never);
}

function plan(sequence: number, base: number | null): RunEvent {
  return event(sequence, 'agent_execution_finished', {
    invocation_id: 'inv',
    agent_kind: 'orchestrator',
    span_id: 'turn',
    data: {
      kind: 'agent_execution_finished',
      result: '{}',
      reply: {
        kind: 'OrchestratorPlan',
        json_schema: {
          properties: {
            hypothesis_id: {type: 'string'},
            hypothesis: {type: 'string'},
            title: {type: 'string'},
            pass_criteria: {type: 'string'},
            base_round: {anyOf: [{type: 'integer'}, {type: 'null'}]},
          },
          type: 'object',
        },
        value: {
          hypothesis_id: 'fixed-slot-ring',
          hypothesis: 'A ring of slots removes the allocator from the hot path.',
          title: 'Preallocate fixed-size queue slots',
          pass_criteria: 'The benchmark improves.',
          base_round: base,
        },
      },
    },
  } as never);
}

describe('a turn that is live', () => {
  async function mountAt(node: ReactNode): Promise<{
    readonly container: HTMLElement;
    readonly render: (next: ReactNode) => Promise<void>;
  }> {
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(node));
    return {container, render: next => act(async () => root.render(next))};
  }

  test('keeps the same narrative element when events are appended', async () => {
    for (const status of ['running', 'done'] as const) {
      const before = fixture([...TURN, reply(18)], status);
      const after = fixture(
        [
          ...TURN,
          reply(18),
          chunk(19, 'analysis', '**Next**\n\nMore.'),
          chunk(20, 'assistant', 'Hi.'),
        ],
        status,
      );
      const {container, render} = await mountAt(
        <StepView {...viewProps(before, before.turn, null)} />,
      );
      const narrative = one(container, '.vs-narrative');
      const wrap = one(container, '.vs-narrative-wrap');
      await render(<StepView {...viewProps(after, after.turn, null)} />);
      expect(one(container, '.vs-narrative')).toBe(narrative);
      expect(one(container, '.vs-narrative-wrap')).toBe(wrap);
      expect(narrative.textContent).toContain('More.');
    }
  });
});

describe('the turn’s layout', () => {
  test('ends with the reply as a card, its summary leading the turn', async () => {
    const fx = fixture([...TURN, reply(18)]);
    const container = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    const text = one(container, '.vs-narrative').textContent ?? '';
    expect(text).toContain('Padded the counters.');
    expect(text).toContain('Changed');
    expect(text).toContain('Counters share a cache line.');
    expect(text.indexOf('Counters share')).toBeLessThan(text.indexOf('Padded the counters.'));
    const blocks = all(one(container, '.vs-narrative'), '.vs-turn-result, .vs-thought, .vs-said');
    expect(blocks.at(-1)?.className).toContain('vs-turn-result');
    expect(container.textContent).not.toContain('Show all fields');
    expect(container.textContent).toContain('src/lib.rs');
    const lead = one(container, '.vs-button');
    expect(lead.textContent).toBe('Builder reply: done · Padded the counters.');
    expect(container.firstElementChild?.contains(lead)).toBe(true);
    expect(one(container, '.vs-narrative').contains(lead)).toBe(false);
  });

  test('the reply card reads its prose as Markdown, and the one-line summary as plain words', async () => {
    const fx = fixture([
      ...TURN,
      reply(18, '**Padded** the `counters`:\n\n- [log](https://x.io) shows 2x'),
    ]);
    const container = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    const card = one(container, '.vs-turn-result');
    expect(one(card, '.vs-markdown strong').textContent).toBe('Padded');
    expect(one(card, '.vs-markdown code').textContent).toBe('counters');
    expect(one(card, '.vs-markdown a').textContent).toBe('log');
    const lead = one(container, '.vs-button').textContent ?? '';
    expect(lead).toContain('Padded the counters');
    expect(lead).not.toMatch(/[*`]/);
  });

  test('shows no summary and no card while the turn runs without a reply', async () => {
    const fx = fixture(TURN, 'running');
    const container = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    expect(all(container, '.vs-turn-result')).toHaveLength(0);
    expect(all(container, '.vs-button')).toHaveLength(0);
  });

  test('a turn of thinking and a reply shows its card at once, with no line repeating it', async () => {
    const fx = fixture([chunk(10, 'analysis', 'Counters share a cache line.'), reply(18)]);
    const container = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    expect(all(container, '.vs-turn-result')).toHaveLength(1);
    expect(all(container, '.vs-button').map(each => each.textContent)).not.toContain(
      'Builder reply: done · Padded the counters.',
    );
    expect(one(container, '.vs-turn-result').textContent).toContain('Builder');
    expect(one(container, '.vs-turn-result').textContent).not.toContain('Builder reply');
  });

  test('the plan card ends the orchestrator’s turn, and its base opens that idea', async () => {
    const earlier = labelled(
      'ws0',
      'flow:root#1',
      'workstream',
      'Pad the counters',
      {},
      {
        hypothesis_id: 'h0',
        round: 1,
      },
    );
    const fx = fixture([...TURN, plan(18, 1)], 'done', [earlier]);
    const container = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    const card = one(container, '.vs-turn-result');
    expect(card.textContent).toContain('Preallocate fixed-size queue slots');
    expect(card.textContent).toContain('builds on');
    expect(one(container, '.vs-button').textContent).toBe(
      'Plan: Preallocate fixed-size queue slots · builds on Pad the counters',
    );
    const base = all(card, '.vs-button').find(each => each.textContent === 'Pad the counters');
    if (base === undefined) throw new Error('the base is not a link');
    await click(base);
    expect(fx.zooms).toEqual([{type: 'open-idea', idea: 'ws0'}]);
    expect(card.textContent).not.toContain('Show all fields');
    expect(card.textContent).toContain('fixed-slot-ring');
    expect(card.textContent?.indexOf('The benchmark improves.')).toBeLessThan(
      card.textContent?.indexOf('fixed-slot-ring') ?? 0,
    );
  });

  test('draws reasoning titles in bold, one row per reasoning item', async () => {
    const fx = fixture([
      chunk(
        10,
        'analysis',
        '**Inspecting history**\n\nRead the log.**Scoping the change**\n\nNarrow it.',
      ),
    ]);
    const container = await mount(<StepView {...viewProps(fx, fx.turn, null)} />);
    expect(all(container, '.vs-thought')).toHaveLength(2);
    expect(all(container, '.vs-thought strong').map(each => each.textContent)).toEqual([
      'Inspecting history',
      'Scoping the change',
    ]);
    expect(container.textContent).not.toContain('**');
  });
});
