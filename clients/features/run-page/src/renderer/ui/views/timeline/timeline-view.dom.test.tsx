/**
 * The Timeline through its props, in a DOM: rows in the Ideas list's order, a row opening its
 * idea, a segment its step, the range, the folded failed lane, and an opened idea's strip.
 */

import {afterAll, beforeAll, expect, test} from 'bun:test';
import {EMPTY_LEDGER} from '@vibesys/run-state';
import type {Candidate} from '@vibesys/run-state/candidates';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import type {ZoomEvent} from '../../../../state/index.js';
import {idea, modelOf} from '../../../../state/timeline-fixtures.js';
import {FAILED_DENSE} from '../../../../state/timeline-rows.js';
import {TimelineView} from './timeline-view.js';

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

async function mount(
  node: ReactNode,
): Promise<{container: HTMLElement; unmount: () => Promise<void>}> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(node));
  return {container, unmount: () => act(async () => root.unmount())};
}

function rows(container: HTMLElement): HTMLElement[] {
  return [
    ...container.querySelectorAll('[aria-label="Ideas on the timeline"] [role="option"]'),
  ] as HTMLElement[];
}

function names(container: HTMLElement): (string | null)[] {
  return rows(container).map(row => row.getAttribute('aria-label'));
}

function base(candidates: readonly Candidate[], now: number, events: ZoomEvent[] = []) {
  return {
    model: modelOf(candidates),
    clock: {start: 0, now},
    tokens: EMPTY_LEDGER,
    who: new Map(),
    zoom: (event: ZoomEvent) => events.push(event),
    idea: null,
  };
}

const IDEAS = [
  idea('old', 'passed', 0, 600),
  idea('bad', 'failed', 1000, 200),
  idea('mid', 'passed', 2000, 600),
  idea('live', 'running', 3000, 0),
];

test('rows follow the Ideas list: running first, then newest first', async () => {
  const {container, unmount} = await mount(<TimelineView {...base(IDEAS, 3600)} />);
  expect(names(container)).toEqual(['live', 'mid', 'bad', 'old']);
  await unmount();
});

test('a row opens its idea and a segment opens its step under that idea', async () => {
  const events: ZoomEvent[] = [];
  const {container, unmount} = await mount(<TimelineView {...base(IDEAS, 3600, events)} />);
  const mid = rows(container).find(row => row.getAttribute('aria-label') === 'mid');
  await act(async () => mid?.click());
  const segments = [...(mid?.querySelectorAll('.vs-gantt-seg') ?? [])] as HTMLElement[];
  expect(segments.map(each => each.getAttribute('data-kind'))).toEqual([
    'solid',
    'hatched',
    'solid',
  ]);
  await act(async () => segments[0]?.click());
  expect(events).toEqual([
    {type: 'open-idea', idea: 'mid'},
    {type: 'open-step', step: 'mid-a-t', idea: 'mid'},
  ]);
  await unmount();
});

test('Last hour keeps only the ideas that overlap the last hour', async () => {
  const many = [idea('long-ago', 'passed', 0, 600), idea('recent', 'passed', 90_000, 600)];
  const {container, unmount} = await mount(<TimelineView {...base(many, 90_700)} />);
  expect(names(container)).toEqual(['recent', 'long-ago']);
  const hour = [...container.querySelectorAll('input[type="radio"]')].find(
    each => (each.parentElement?.textContent ?? '') === 'Last hour',
  ) as HTMLInputElement;
  await act(async () => hour.click());
  expect(names(container)).toEqual(['recent']);
  await unmount();
});

test('many failed ideas fold into one lane that opens them', async () => {
  const dense = [
    idea('passed-one', 'passed', 0, 100),
    ...Array.from({length: FAILED_DENSE + 1}, (_, at) => idea(`r${at}`, 'failed', at * 10, 50)),
  ];
  const events: ZoomEvent[] = [];
  const {container, unmount} = await mount(<TimelineView {...base(dense, 1000, events)} />);
  expect(names(container)).toEqual([`+ ${FAILED_DENSE + 1} failed`, 'passed-one']);
  await act(async () => rows(container)[0]?.click());
  // The list is windowed, so what shows is the lane gone and the way to fold it back.
  expect(names(container)).not.toContain(`+ ${FAILED_DENSE + 1} failed`);
  expect(container.textContent).toContain(`Fold ${FAILED_DENSE + 1} failed`);
  expect(events).toEqual([]);
  await unmount();
});

test('an opened idea shows the strip of its latest attempt; a run with no ideas says so', async () => {
  const events: ZoomEvent[] = [];
  const candidate = idea('mid', 'passed', 2000, 600);
  const opened = {...base([candidate], 3600, events), idea: candidate};
  const {container, unmount} = await mount(<TimelineView {...opened} />);
  const segments = [...container.querySelectorAll('.vs-gantt-seg')] as HTMLElement[];
  await act(async () => segments[2]?.click());
  expect(events).toEqual([{type: 'open-step', step: 'mid-a-j'}]);
  await unmount();

  const empty = await mount(<TimelineView {...base([], 100)} />);
  expect(empty.container.textContent).toContain('No ideas yet');
  await empty.unmount();
});

test('a long idea name is in full on the row’s name, for hover and assistive tech', async () => {
  const long = 'Per-core queues with work stealing and a batched, padded, cache-aligned ring';
  const {container, unmount} = await mount(
    <TimelineView {...base([idea(long, 'passed', 0, 600)], 700)} />,
  );
  expect(container.querySelector('[role="option"] .vs-gantt-name')?.getAttribute('title')).toBe(
    long,
  );
  expect(names(container)).toEqual([long]);
  await unmount();
});

test('a failed idea has no multiple to show, so its cell is a dash, as in the Ideas list', async () => {
  const long = {...idea('long', 'failed', 0, 100), reason: 'implementer_gave_up'};
  const checked = idea('checked', 'failed', 200, 100);
  const failing = {
    ...checked,
    measurement: {
      span: 'g',
      at: 300,
      metricValue: null,
      vsBaseline: null,
      against: null,
      readings: [],
      inputReadings: [],
      verdict: 'failed_check',
      failed: {check: 'correctness', completed: 93, required: 100, unit: 'trials'},
    },
  };
  const fits = idea('fits', 'passed', 400, 100);
  const {container, unmount} = await mount(<TimelineView {...base([long, failing, fits], 600)} />);
  const figures = [...container.querySelectorAll('[role="option"] .vs-gantt-figure')].map(
    each => each.textContent,
  );
  expect(figures).toEqual(['1.50×', '–', '–']);
  await unmount();
});

test('a legend names each role on the bars, in the color of its segments, and the gates', async () => {
  const {container, unmount} = await mount(<TimelineView {...base(IDEAS, 3600)} />);
  const legend = container.querySelector('[aria-label="Bar colors"]');
  const entries = [...(legend?.querySelectorAll('li') ?? [])];
  expect(entries.map(each => each.textContent ?? '')).toEqual(['Builder', 'Reviewer', 'Gate']);
  const colors = entries.map(item =>
    (item.querySelector('.vs-series-swatch') as HTMLElement).style.getPropertyValue('--seg-color'),
  );
  const bar = rows(container)[0]?.querySelectorAll('.vs-gantt-seg') ?? [];
  const drawn = [...bar].map(each => (each as HTMLElement).style.getPropertyValue('--seg-color'));
  expect(drawn.filter(color => color !== '').sort()).toEqual(colors.slice(0, 2).sort());
  await unmount();
});

test('the legend names a quota pause when a bar shows one, and only then', async () => {
  const legend = (container: HTMLElement) =>
    [...container.querySelectorAll('[aria-label="Bar colors"] li')].map(item => item.textContent);
  const quiet = await mount(<TimelineView {...base(IDEAS, 3600)} />);
  expect(legend(quiet.container)).not.toContain('Quota pause');
  await quiet.unmount();
  // The idea `mid` ran 2000 to 2600: a pause inside it cuts its bar and joins the legend.
  const pause = {from: 2100, to: 2300, provider: 'claude', condition: 'quota_exhausted' as const};
  const paused = {...base(IDEAS, 3600), model: {...modelOf(IDEAS), pauses: [pause]}};
  const shown = await mount(<TimelineView {...paused} />);
  expect(legend(shown.container)).toContain('Quota pause');
  await shown.unmount();
});
