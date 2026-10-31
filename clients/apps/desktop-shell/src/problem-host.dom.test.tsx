import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {boardToasts, createProblemBoard, type Problem} from '@vibesys/problems';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {ProblemHost, useReportedProblems} from './problem-host.js';

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

const SAVE: Problem = {
  id: 'save',
  tone: 'err',
  title: 'Could not save the change',
  detail: 'The disk is full.',
};

function Screen(props: {
  readonly board: ReturnType<typeof createProblemBoard>;
  readonly problems: readonly Problem[];
}) {
  useReportedProblems(props.board, 'screen', props.problems);
  return <p>The screen</p>;
}

function setup() {
  const board = createProblemBoard();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const show = (problems: readonly Problem[], mounted = true) =>
    act(async () =>
      root.render(
        <ProblemHost board={board}>
          {mounted ? <Screen board={board} problems={problems} /> : <p>Another screen</p>}
        </ProblemHost>,
      ),
    );
  const button = (name: string) => {
    const found = [...container.querySelectorAll('button')].find(
      each => each.getAttribute('aria-label') === name || each.textContent === name,
    );
    if (found === undefined) throw new Error(`no button ${name}`);
    return found;
  };
  return {board, container, show, button};
}

describe('the window’s host of problems', () => {
  test('shows a reported problem over the screen, collapsed, and follows the screen', async () => {
    const {container, show} = setup();
    await show([]);
    expect(container.textContent).toContain('The screen');
    expect(container.querySelector('[role="alert"]')).toBeNull();
    await show([SAVE]);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      'Could not save the change',
    );
    expect(container.textContent).not.toContain('The disk is full.');
    await show([]);
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  test('opens a toast to its detail and dismisses it until the problem clears and recurs', async () => {
    const {container, show, button} = setup();
    await show([SAVE]);
    await act(async () => button('Show details').click());
    expect(container.textContent).toContain('The disk is full.');
    await act(async () => button('Dismiss').click());
    expect(container.querySelector('[role="alert"]')).toBeNull();
    await show([{...SAVE, detail: 'The disk is still full.'}]);
    expect(container.querySelector('[role="alert"]')).toBeNull();
    await show([]);
    await show([SAVE]);
    expect(container.querySelector('[role="alert"]')).not.toBeNull();
    expect(container.textContent).not.toContain('The disk is full.');
  });

  test('retracts what a screen reported when the screen goes away', async () => {
    const {board, show} = setup();
    await show([SAVE]);
    expect(boardToasts(board.getState())).toHaveLength(1);
    await show([SAVE], false);
    expect(boardToasts(board.getState())).toHaveLength(0);
  });
});
