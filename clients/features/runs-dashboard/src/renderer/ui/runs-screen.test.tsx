import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {type MachineSteps, RunsScreen} from './runs-screen.js';

function screen(loads: readonly MachineSteps[], machine: string | null = null): string {
  return renderToStaticMarkup(
    <RunsScreen
      tiles={[]}
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
