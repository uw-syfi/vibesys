import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import type {RunControl} from './model.js';
import {filterPalette, homePaletteItems, type PaletteInput, paletteItems} from './palette.js';
import type {RoundRow} from './rounds.js';

const row = (round: number, title: string): RoundRow => ({
  round,
  state: 'kept',
  title,
  hypothesis: null,
  value: null,
  delta: null,
  before: {value: null, round: 0},
});
const PAUSE: RunControl = {
  kind: 'action',
  action: 'pause',
  label: 'Pause',
  tip: '',
  disabled: false,
};
const BASE: PaletteInput = {
  control: PAUSE,
  pending: false,
  canStop: true,
  canSteer: true,
  hasRunId: true,
  rows: [row(1, 'Batch decode steps'), row(2, 'Grow the KV cache block')],
  live: 2,
  selected: null,
  pane: null,
  sidebarShown: true,
  newRun: false,
  resumeHref: null,
  prompt: {turn: 'x1', detail: 'round 2, attempt 1'},
  todos: {turn: 'x1', detail: 'round 2'},
};
const labels = (input: PaletteInput) =>
  paletteItems(input).map(item => `${item.group}: ${item.label}`);

test('the palette mirrors the visible controls', () => {
  assert.deepEqual(labels(BASE), [
    'Run: Pause after the current agent call',
    'Run: Stop run…',
    'Run: Steer the next agent call',
    'Run: Copy run ID',
    'Go to: Round 1',
    'Go to: Round 2',
    'Go to: Ask',
    'Go to: Changes',
    'Go to: Agents',
    'Go to: Experiments',
    'Go to: Notes',
    'Go to: Hide sidebar',
    'Agent: Show the prompt',
    'Agent: Show the todos',
  ]);
});

test('what is not visible is not offered: an ended run, a pending transition, no prompt', () => {
  const ended = labels({
    ...BASE,
    control: {kind: 'ended', word: 'Completed', summary: null, tip: null},
    canStop: false,
    canSteer: false,
    prompt: null,
    todos: null,
  });
  assert.deepEqual(
    ended.filter(label => label.startsWith('Run:')),
    ['Run: Copy run ID'],
  );
  assert.equal(
    ended.some(label => label.startsWith('Agent:')),
    false,
  );
  assert.equal(
    labels({...BASE, pending: true}).includes('Run: Pause after the current agent call'),
    false,
  );
  const paused = labels({...BASE, control: {...PAUSE, action: 'resume', label: 'Resume'}});
  assert.equal(paused[0], 'Run: Resume the run');
  assert.ok(labels({...BASE, sidebarShown: false}).includes('Go to: Show sidebar'));
  const here = labels({...BASE, selected: 2, pane: 'changes'});
  assert.equal(here.includes('Go to: Round 2'), false);
  assert.equal(here.includes('Go to: Changes'), false);
  assert.ok(here.includes('Go to: Round 1'));
});

test('filtering matches group, label and detail, ignoring case', () => {
  const items = paletteItems(BASE);
  assert.deepEqual(
    filterPalette(items, 'kv cache').map(item => item.label),
    ['Round 2'],
  );
  assert.deepEqual(
    filterPalette(items, 'AGENT: show the p').map(item => item.label),
    ['Show the prompt'],
  );
  assert.equal(filterPalette(items, '  ').length, items.length);
});

test('New run is offered with its shortcut only when the sidebar shows it', () => {
  const entry = paletteItems({...BASE, newRun: true}).find(item => item.id === 'run-new');
  assert.deepEqual(
    entry && {group: entry.group, label: entry.label, keys: entry.keys, intent: entry.intent},
    {
      group: 'Run',
      label: 'New run',
      keys: '⌘N',
      intent: {kind: 'newRun'},
    },
  );
  assert.equal(labels(BASE).includes('Run: New run'), false);
});

test('Resume run… mirrors the ••• menu: offered only with a resume link', () => {
  const entry = paletteItems({...BASE, resumeHref: '/?token=h#resume=p1/r1'}).find(
    item => item.id === 'run-resume',
  );
  assert.deepEqual(entry && {group: entry.group, label: entry.label, intent: entry.intent}, {
    group: 'Run',
    label: 'Resume run…',
    intent: {kind: 'open', href: '/?token=h#resume=p1/r1'},
  });
  assert.equal(labels(BASE).includes('Run: Resume run…'), false);
});

test('the home palette: New run unless it is open, and every run the sidebar links', () => {
  const run = (id: string, url: string | null) => ({
    id,
    projectId: 'p1',
    title: `Run ${id}`,
    gateway: 'none' as const,
    outcome: 'completed' as const,
    updatedAt: '2026-09-28T12:00:00Z',
    url,
    loop: 'agent',
  });
  const sections = [
    {id: 'p1', name: 'llm-serve', runs: [run('a', '/?token=h#open=p1/a'), run('b', null)]},
  ];
  const items = homePaletteItems(sections, true);
  assert.deepEqual(
    items.map(entry => [entry.label, entry.detail, entry.keys]),
    [
      ['New run', '', '⌘N'],
      ['Run a', 'llm-serve', undefined],
    ],
  );
  assert.deepEqual(items[1]?.intent, {kind: 'open', href: '/?token=h#open=p1/a'});
  assert.deepEqual(
    homePaletteItems(sections, false).map(entry => entry.label),
    ['Run a'],
  );
});
