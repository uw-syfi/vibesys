import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {tileOf} from '../../state/index.js';
import {type MachineSteps, RunsScreen} from './runs-screen.js';

const TILE = tileOf({
  id: 'run-1',
  runId: 'run-1',
  title: 'run-1',
  started: null,
  machine: 'host-b',
  hardware: null,
  status: {kind: 'running', word: 'running'},
  problem: null,
  reason: null,
  ideas: null,
  costUsd: null,
  seconds: null,
  best: null,
  measured: null,
  createdAt: 1,
  points: [],
  pointsOmitted: 0,
  attention: 'none',
  failedStreak: 0,
  silentSeconds: null,
});

function screen(
  loads: readonly MachineSteps[],
  machine: string | null = null,
  tiles = false,
): string {
  return renderToStaticMarkup(
    <RunsScreen
      tiles={tiles ? [TILE] : []}
      by="status"
      machine={machine}
      clearMachine={() => {}}
      group={() => {}}
      open={() => {}}
      menu={() => []}
      draft={null}
      loads={loads}
      newRun={() => {}}
      notice={null}
    />,
  );
}

const load = (name: string, retry: (() => void) | null): MachineSteps => ({
  machine: name,
  steps: [{id: 'gateway', label: 'Starting gateway', state: 'current', position: '1/3'}],
  retry,
});

test('with no run and every machine listed the screen offers New run', () => {
  const html = screen([]);
  expect(html).toContain('No runs');
  expect(html).toContain('New run');
  expect(html).not.toContain('vs-step-list');
});

test('each machine that has not listed has its own block, and No runs waits for them', () => {
  const html = screen([load('This Mac', null), load('host-a', null)]);
  expect(html).toContain('This Mac');
  expect(html).toContain('host-a');
  expect(html.match(/vs-step-list/g)).toHaveLength(2);
  expect(html).toContain('1/3');
  expect(html).not.toContain('Retry');
  expect(html).not.toContain('No runs');
});

test('a stopped machine offers Retry', () => {
  expect(screen([load('host-a', () => {})])).toContain('Retry');
});

test('narrowed to a machine, only its block shows', () => {
  const html = screen([load('This Mac', null), load('host-a', null)], 'host-a');
  expect(html.match(/vs-step-list/g)).toHaveLength(1);
});

test('once runs show, a machine still loading or stopped keeps a line, not a block of steps', () => {
  const html = screen([load('This Mac', null), load('host-a', () => {})], null, true);
  expect(html).toContain('run-1');
  expect(html).not.toContain('vs-step-list');
  expect(html).toContain('This Mac');
  expect(html).toContain('host-a');
  expect(html).toContain('Starting gateway 1/3');
  expect(html.match(/Retry/g)).toHaveLength(1);
  expect(screen([], null, true)).not.toContain('Loading');
});
