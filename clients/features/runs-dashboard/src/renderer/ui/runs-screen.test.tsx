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
  notice: {readonly title: string; readonly body: string} | null = null,
): string {
  return renderToStaticMarkup(
    <RunsScreen
      settled="ready"
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
      notice={notice}
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

test('saved runs that could not be read are a status word beside the title, the detail as its description', () => {
  const html = screen([], null, false, {
    title: '2 saved runs skipped',
    body: 'recent.json: runs[16].task must be a task name',
  });
  expect(html).toContain('>2 saved runs skipped<');
  expect(html).toContain('aria-description="recent.json: runs[16].task must be a task name"');
  expect(html).not.toContain('>recent.json');
  expect(html).not.toContain('vs-banner');
  expect(screen([])).not.toContain('saved runs skipped');
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
