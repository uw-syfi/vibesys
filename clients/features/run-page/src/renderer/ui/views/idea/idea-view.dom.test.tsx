/**
 * The idea body through its props, in a DOM: attempts (the latest selected until another is
 * chosen), the strip and the steps of the selected attempt, each opening its step; the compact
 * step list; and the header's facts.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {EMPTY_LEDGER} from '@vibesys/run-state';
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import type {ZoomEvent} from '../../../../state/index.js';
import {attempt, idea, modelOf, step} from '../../../../state/timeline-fixtures.js';
import {ledgerOf} from '../../../../state/usage-fixtures.js';
import {IdeaView, ideaHeader, StepList} from './idea-view.js';

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

const CLOCK = {start: 0, now: 1000};

async function mount(
  node: ReactNode,
): Promise<{container: HTMLElement; root: Root; unmount: () => Promise<void>}> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(node));
  return {container, root, unmount: () => act(async () => root.unmount())};
}

function options(container: HTMLElement, name: string): HTMLElement[] {
  const list = [...container.querySelectorAll('[role="listbox"]')].find(
    each => each.getAttribute('aria-label') === name,
  );
  return list === undefined ? [] : ([...list.querySelectorAll('[role="option"]')] as HTMLElement[]);
}

function listNames(container: HTMLElement): string[] {
  return [...container.querySelectorAll('[role="listbox"]')].map(
    each => each.getAttribute('aria-label') ?? '',
  );
}

function tried(): Candidate {
  const base = idea('mpmc', 'passed', 100, 300);
  return {
    ...base,
    attempts: [
      attempt('a1', 'failed', 100, 60),
      attempt('a2', 'passed', 160, 120),
      attempt('a3', 'passed', 280, 120),
    ],
  };
}

function view(candidate: Candidate, zoom: (event: ZoomEvent) => void) {
  return {
    model: modelOf([candidate]),
    clock: CLOCK,
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom,
    idea: candidate,
    outcome: null,
    effect: null,
  };
}

test('a one-attempt idea shows its steps, role first, and a step opens', async () => {
  const events: ZoomEvent[] = [];
  const candidate = idea('mpmc', 'passed', 100, 300);
  const {container, unmount} = await mount(<IdeaView {...view(candidate, e => events.push(e))} />);
  expect(listNames(container).some(name => name === 'Attempts')).toBe(false);
  const steps = options(container, 'mpmc steps');
  expect(steps.map(row => row.textContent)).toEqual([
    expect.stringContaining('Builder'),
    expect.stringContaining('Gate'),
    expect.stringContaining('Reviewer'),
  ]);
  expect(steps[0]?.textContent).toContain('1m');
  await act(async () => steps[1]?.click());
  expect(events).toEqual([{type: 'open-step', step: 'mpmc-a-g'}]);
  await unmount();
});

test('the latest attempt is selected; choosing another shows its steps', async () => {
  const events: ZoomEvent[] = [];
  const candidate = tried();
  const {container, unmount} = await mount(<IdeaView {...view(candidate, e => events.push(e))} />);
  const lines = options(container, 'Attempts');
  expect(lines.map(row => row.textContent)).toEqual([
    expect.stringContaining('Failed'),
    expect.stringContaining('1.50×'),
    expect.stringContaining('1.50×'),
  ]);
  expect(lines.map(row => row.getAttribute('aria-selected'))).toEqual(['false', 'false', 'true']);
  expect(options(container, 'Attempt 3 steps').length).toBe(3);

  await act(async () => lines[0]?.click());
  expect(options(container, 'Attempt 3 steps')).toEqual([]);
  const first = options(container, 'Attempt 1 steps');
  await act(async () => first[0]?.click());
  expect(events).toEqual([{type: 'open-step', step: 'a1-t'}]);
  await unmount();
});

test('a segment of the strip opens its step', async () => {
  const events: ZoomEvent[] = [];
  const {container, unmount} = await mount(<IdeaView {...view(tried(), e => events.push(e))} />);
  const segments = [...container.querySelectorAll('.vs-gantt-seg')] as HTMLElement[];
  expect(segments.map(each => each.getAttribute('data-kind'))).toEqual([
    'solid',
    'hatched',
    'solid',
  ]);
  await act(async () => segments[1]?.click());
  expect(events).toEqual([{type: 'open-step', step: 'a3-g'}]);
  await unmount();
});

test('the compact step list highlights the opened step', async () => {
  const candidate = tried();
  const {container, unmount} = await mount(
    <StepList
      idea={candidate}
      selected="a3-g"
      tokens={EMPTY_LEDGER}
      who={new Map()}
      pauses={[]}
      zoom={() => undefined}
    />,
  );
  const rows = options(container, 'mpmc steps');
  expect(rows.map(row => row.getAttribute('aria-selected'))).toEqual(['false', 'true', 'false']);
  await unmount();
});

test('an adoption step shows the recorded checkout headline', async () => {
  const candidate = idea('mpmc', 'passed', 100, 300);
  const adopt = step('adopt-1', 'adoption', null, 100, 40);
  const shown = {
    ...candidate,
    attempts: candidate.attempts.map((each, index) =>
      index === 0 ? {...each, steps: [adopt]} : each,
    ),
  };
  const titles = new Map([['adopt-1', 'Adopt candidate abc1234 · Dirty tree']]);
  const {container, unmount} = await mount(
    <IdeaView {...view(shown, () => undefined)} adoptionTitles={titles} />,
  );
  expect(
    options(container, 'mpmc steps')
      .map(row => row.textContent)
      .join('\n'),
  ).toContain('Adopt candidate abc1234 · Dirty tree');
  expect(container.textContent).not.toContain('adoption adopt-1');
  await unmount();

  const compact = await mount(
    <StepList
      idea={shown}
      selected={null}
      tokens={EMPTY_LEDGER}
      who={new Map()}
      pauses={[]}
      adoptionTitles={titles}
      zoom={() => undefined}
    />,
  );
  expect(
    options(compact.container, 'mpmc steps')
      .map(row => row.textContent)
      .join('\n'),
  ).toContain('Adopt candidate abc1234 · Dirty tree');
  await compact.unmount();
});

test('an idea with no attempts has no steps to show', async () => {
  const candidate = {...idea('mpmc', 'running', 100, 0), attempts: []};
  const {container, unmount} = await mount(<IdeaView {...view(candidate, () => undefined)} />);
  expect(container.textContent).toContain('No steps yet');
  await unmount();
});

test('the header reads outcome and duration without lineage', async () => {
  const candidate = {...idea('mpmc', 'passed', 100, 300), parent: 'base'};
  const props = view(candidate, () => undefined);
  const meta = ideaHeader({
    ...props,
    model: modelOf([idea('base', 'passed', 0, 100), candidate]),
  }).meta;
  const {container, unmount} = await mount(meta);
  expect(container.textContent).toBe('Passed5m');
  await unmount();
});

test('shows the implementer outcome as a chip and its summary in the idea view', async () => {
  const candidate = idea('mpmc', 'passed', 100, 300);
  const props = {
    ...view(candidate, () => undefined),
    outcome: {outcome: {label: 'blocked', tone: 'err' as const}, summary: 'Padded the counters.'},
  };
  const {container, unmount} = await mount(<IdeaView {...props} />);
  expect(container.querySelector('.vs-chip')?.textContent).toBe('blocked');
  expect(container.textContent).toContain('Padded the counters.');
  await unmount();
});

test('an implementer that reported no outcome still shows the idea’s own, with its summary', async () => {
  const candidate = {...idea('mpmc', 'failed', 100, 300), reason: 'not_measured'};
  const props = {
    ...view(candidate, () => undefined),
    outcome: {outcome: null, summary: 'Tried a padded head.'},
  };
  const {container, unmount} = await mount(<IdeaView {...props} />);
  expect(container.querySelector('.vs-chip')?.textContent).toBe('Failed');
  expect(container.textContent).toContain('Tried a padded head.');
  await unmount();
});

/** An idea whose one attempt has these steps: [role or null for a gate, title, status]. */
function withSteps(
  steps: readonly (readonly [string | null, string, CandidateStep['status']])[],
): Candidate {
  const base = idea('mpmc', 'passed', 100, 300);
  const first = base.attempts[0];
  if (first === undefined) throw new Error('fixture');
  const made = steps.map(([role, title, status], at) => ({
    ...step(`s${at}`, role === null ? 'gate' : 'agent_turn', role, 100 + at * 50, 40),
    title,
    status,
  }));
  return {...base, attempts: [{...first, steps: made}]};
}

function colorsOf(nodes: Iterable<Element>): string[] {
  return [...nodes].map(node => (node as HTMLElement).style.getPropertyValue('--seg-color'));
}

test('the strip is keyed by a legend of exactly the roles in it, in the segments colors', async () => {
  const candidate = withSteps([
    ['orchestrator', 'orchestrator', 'done'],
    ['implementer', 'Padded the counters', 'done'],
    ['orchestrator', 'orchestrator', 'done'],
    [null, 'gate', 'done'],
  ]);
  const {container, unmount} = await mount(<IdeaView {...view(candidate, () => undefined)} />);
  const legend = container.querySelector('ul[aria-label="Step colors"]');
  expect(legend).not.toBeNull();
  const entries = [...(legend?.querySelectorAll('li') ?? [])];
  expect(entries.map(each => each.textContent)).toEqual(['Builder', 'Planner', 'Gate']);
  const swatches = entries.map(each => each.querySelector('.vs-series-swatch') as HTMLElement);
  expect(swatches.map(each => each.getAttribute('data-hatched') !== null)).toEqual([
    false,
    false,
    true,
  ]);
  const [implementer, orchestrator] = colorsOf(swatches);
  expect(colorsOf(container.querySelectorAll('.vs-gantt-seg'))).toEqual([
    orchestrator as string,
    implementer as string,
    orchestrator as string,
    '',
  ]);
  await unmount();
});

test('a strip without a gate has no gate entry, and a role is listed once', async () => {
  const candidate = withSteps([
    ['judge', 'judge', 'done'],
    ['judge', 'judge', 'done'],
  ]);
  const {container, unmount} = await mount(<IdeaView {...view(candidate, () => undefined)} />);
  const legend = container.querySelector('ul[aria-label="Step colors"]');
  expect([...(legend?.querySelectorAll('li') ?? [])].map(each => each.textContent)).toEqual([
    'Reviewer',
  ]);
  await unmount();
});

test('a step row names its role once, in its strip color, with status only when unusual', async () => {
  const candidate = withSteps([
    ['orchestrator', 'orchestrator', 'done'],
    ['implementer', 'Padded the counters', 'done'],
    ['judge', 'judge', 'failed'],
    [null, 'gate', 'done'],
  ]);
  const {container, unmount} = await mount(<IdeaView {...view(candidate, () => undefined)} />);
  const rows = options(container, 'mpmc steps');
  const occurrences = (row: HTMLElement | undefined, word: string): number =>
    (row?.textContent ?? '').split(word).length - 1;
  expect(occurrences(rows[0], 'Planner')).toBe(1);
  expect(occurrences(rows[1], 'Builder')).toBe(1);
  expect(rows[1]?.textContent).toContain('Padded the counters');
  expect(occurrences(rows[2], 'Reviewer')).toBe(1);
  expect(occurrences(rows[3], 'Gate')).toBe(1);
  const swatches = rows.map(row => row.querySelector('.vs-series-swatch') as HTMLElement);
  expect(colorsOf(swatches)).toEqual(colorsOf(container.querySelectorAll('.vs-gantt-seg')));
  expect(swatches[3]?.getAttribute('data-hatched')).not.toBeNull();
  // Status shows only for the failed step, as a badge, and no row has a status dot.
  expect(rows.map(row => row.querySelector('.vs-chip')?.textContent ?? null)).toEqual([
    null,
    null,
    'Failed',
    null,
  ]);
  expect(rows.some(row => row.querySelector('.vs-pickrow-dot') !== null)).toBe(false);
  await unmount();
});

test('an agent turn’s step shows its tokens beside its duration; a gate shows none and a quiet turn says unknown', async () => {
  const candidate = idea('mpmc', 'passed', 100, 300);
  const tokens = ledgerOf([
    {span: 'mpmc-a-t', input: 40_000, cached: 30_000, output: 8_000, reasoning: 3_000},
    {span: 'mpmc-a-g', input: 999},
  ]);
  const {container, unmount} = await mount(
    <IdeaView {...view(candidate, () => undefined)} tokens={tokens} />,
  );
  const steps = options(container, 'mpmc steps');
  const figure = (row: HTMLElement | undefined) => row?.querySelector('.vs-pickrow-measure');
  expect(figure(steps[0])?.textContent).toBe('in 40k (30k cached) · out 8k');
  expect(figure(steps[0])?.getAttribute('title')).toBe(
    'Input 40,000 (30,000 cached, 10,000 uncached)\nOutput 8,000 (3,000 reasoning)\nTotal 48,000',
  );
  expect(steps[0]?.textContent).toContain('1m');
  // A gate keeps a blank cell, so the figures line up.
  expect(figure(steps[1])?.textContent).toBe('');
  // A turn that reported nothing says so, never a zero.
  expect(figure(steps[2])?.textContent).toBe('unknown');
  await unmount();
});

test('the implementer and judge rows say what ran them; a turn the records do not name says nothing', async () => {
  const candidate = idea('mpmc', 'passed', 100, 300);
  const who = new Map([
    ['mpmc-a-t', {harness: 'codex', model: 'gpt-6-luna', client: null}],
    ['mpmc-a-j', {harness: 'claude', model: 'sonnet-5', client: null}],
  ]);
  const {container, unmount} = await mount(<IdeaView {...{...view(candidate, () => {}), who}} />);
  const steps = options(container, 'mpmc steps').map(row => row.textContent ?? '');
  expect(steps[0]).toContain('gpt-6-luna on codex');
  expect(steps[1]).not.toContain(' on ');
  expect(steps[2]).toContain('sonnet-5 on claude');
  await unmount();
});
