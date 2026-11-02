/**
 * The Runs dashboard in a browser DOM, through its public props: the Group by control reads as a
 * labelled setting offering exactly Status and Machine, and choosing an option reports it; a run
 * that needs its user is a card that says why and offers Run again once it has ended.
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
  again: string[] = [],
): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () => {
    createRoot(container).render(
      <RunsScreen
        settled="ready"
        tiles={runs.map(tileOf)}
        by={by}
        machine={null}
        clearMachine={() => undefined}
        group={next => chosen.push(next)}
        open={() => undefined}
        again={id => again.push(id)}
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

describe('the runs that need their user', () => {
  const failed: RunInput = {
    ...run('f', 'host-a'),
    status: {kind: 'failed', word: 'failed'},
    attention: 'failed',
    reason: 'NestedRepositoryError: a repository inside the project',
    ideas: 3,
    seconds: 2400,
  };
  const quiet: RunInput = {
    ...run('q', 'host-b'),
    status: {kind: 'running', word: 'running'},
    attention: 'stalled',
    silentSeconds: 32 * 60,
  };

  function cards(container: HTMLElement): HTMLElement[] {
    return [...container.querySelectorAll<HTMLElement>('.vs-attention-card')];
  }

  test('each is a card with a pill and a sentence saying why; the others stay tiles', async () => {
    const container = await mount('status', [], [failed, quiet, run('ok', 'host-a')]);
    const found = cards(container);
    expect(found.map(card => card.querySelector('.vs-chip')?.textContent)).toEqual([
      'Failed',
      'Quiet 32m',
    ]);
    expect(found[0]?.textContent).toContain(
      'Nested repository error: a repository inside the project',
    );
    expect(found[1]?.textContent).toContain('Still running, but it has recorded nothing for 32m.');
    expect(container.querySelectorAll('.vs-overview-tile:not(.vs-attention-card)')).toHaveLength(1);
  });

  test('Run again is offered to the ended run only, and reports which run', async () => {
    const again: string[] = [];
    const container = await mount('status', [], [failed, quiet], again);
    const [ended, working] = cards(container);
    const buttons = (card: HTMLElement | undefined): HTMLElement[] =>
      [...(card?.querySelectorAll<HTMLElement>('button') ?? [])].filter(
        button => button.textContent === 'Run again',
      );
    expect(buttons(working)).toHaveLength(0);
    const press = buttons(ended)[0];
    expect(press).toBeDefined();
    await act(async () => {
      press?.dispatchEvent(new window.MouseEvent('click', {bubbles: true}) as unknown as Event);
    });
    expect(again).toEqual(['f']);
  });
});
