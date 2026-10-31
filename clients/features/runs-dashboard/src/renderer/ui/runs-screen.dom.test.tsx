/**
 * The Runs dashboard in a browser DOM, through its public props: the Group by control reads as a
 * labelled setting offering exactly Status and Machine, and choosing an option reports it.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {GROUP_BYS, type GroupBy, type RunInput, tileOf} from '../../state/index.js';
import {RunsScreen} from './runs-screen.js';

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

function run(id: string, machine: string): RunInput {
  return {
    id,
    runId: id,
    title: id,
    started: null,
    machine,
    hardware: null,
    status: {kind: 'finished', word: 'finished'},
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
  };
}

async function mount(
  by: GroupBy,
  chosen: GroupBy[],
  runs: readonly RunInput[] = [run('a', 'host-a'), run('b', 'host-b')],
): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () => {
    createRoot(container).render(
      <RunsScreen
        tiles={runs.map(tileOf)}
        by={by}
        machine={null}
        clearMachine={() => undefined}
        group={next => chosen.push(next)}
        open={() => undefined}
        menu={() => []}
        draft={null}
        loads={[]}
        newRun={() => undefined}
        notice={null}
      />,
    );
  });
  return container;
}

function select(container: HTMLElement): HTMLSelectElement {
  const element = container.querySelector('select');
  if (element === null) throw new Error('no Group by select');
  return element;
}

describe('the Group by control', () => {
  test('shows its label before the chosen option, and the label names the select', async () => {
    const container = await mount('status', []);
    const label = container.querySelector('.vs-select-label');
    expect(label?.textContent).toBe('Group by');
    expect(select(container).labels?.[0]?.textContent).toBe('Group by');
    expect(select(container).selectedOptions[0]?.textContent).toBe('Status');
  });

  test('offers exactly Status and Machine, one for each grouping the screen has', async () => {
    const container = await mount('status', []);
    const options = [...select(container).options];
    expect(options.map(option => option.textContent)).toEqual(['Status', 'Machine']);
    expect(options.map(option => option.value)).toEqual([...GROUP_BYS]);
  });

  test('reports the option chosen', async () => {
    const chosen: GroupBy[] = [];
    const container = await mount('status', chosen);
    await act(async () => {
      const control = select(container);
      control.value = 'machine';
      control.dispatchEvent(new window.Event('change', {bubbles: true}) as unknown as Event);
    });
    expect(chosen).toEqual(['machine']);
  });

  test('is left out, with the chart key, when there is no run to group', async () => {
    const container = await mount('status', [], []);
    expect(container.querySelector('select')).toBeNull();
    expect(container.querySelector('[aria-label="Chart key"]')).toBeNull();
  });

  test('keeps the chart key on the title row, apart from the control', async () => {
    const container = await mount('machine', []);
    const key = container.querySelector('[aria-label="Chart key"]');
    const control = container.querySelector('.vs-select');
    expect(key).not.toBeNull();
    expect(key?.contains(control)).toBe(false);
    expect(key?.parentElement?.contains(control)).toBe(false);
  });
});
