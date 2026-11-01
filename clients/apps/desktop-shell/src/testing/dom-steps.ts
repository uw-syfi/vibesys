/**
 * The steps of `settings-pages.ts` read with hands in a DOM: each moves the shell the way a person
 * does (press, pick, type) and returns once the screen has taken it in.
 */
import {act} from 'react';
import {fillIn, find, pickLabel, press} from './dom-controls.js';
import type {Step} from './settings-pages.js';
import type {OpenShell} from './shell-world.js';

/** How many turns of the event loop a step gets to see its result before the test says it never came. */
const TURNS = 64;

async function until(what: string, held: () => boolean): Promise<void> {
  for (let turn = 0; turn < TURNS; turn += 1) {
    if (held()) return;
    await act(async () => Promise.resolve());
  }
  throw new Error(`${what} did not appear in ${TURNS} turns`);
}

const textOf = (element: Element | null): string => (element?.textContent ?? '').trim();

/** The text an element holds itself, not what the elements inside it hold. */
const ownText = (element: Element): string =>
  [...element.childNodes]
    .filter(node => node.nodeType === 3)
    .map(node => node.textContent ?? '')
    .join('')
    .trim();

async function take(shell: OpenShell, step: Step): Promise<void> {
  const {container} = shell;
  const main = container.querySelector('main');
  if (main === null) throw new Error('the page has no main landmark');
  if ('open' in step) {
    const nav = container.querySelector('nav[aria-label="Settings sections"]');
    if (nav === null) throw new Error('the page has no Settings sections');
    await press(find(nav, {role: 'button', name: step.open, nth: 0}));
    if (step.machine !== undefined) {
      const machine = find(nav, {role: 'combobox', name: 'Machine', nth: 0});
      await pickLabel(machine as HTMLSelectElement, step.machine);
    }
    await shell.read();
    await until(`the ${step.open} heading`, () =>
      [...main.querySelectorAll('h1')].some(each => textOf(each) === step.open),
    );
  } else if ('press' in step) {
    await press(find(main, {role: step.role, name: step.press, nth: 0}));
  } else if ('choose' in step) {
    await press(find(main, {role: 'radio', name: step.choose, nth: 0}));
  } else if ('fill' in step) {
    await fillIn(
      find(main, {role: 'textbox', name: step.fill, nth: 0}) as HTMLInputElement,
      step.text,
    );
  } else {
    const found = (): boolean =>
      step.heading === true
        ? [...main.querySelectorAll('h1, h2, h3, h4')].some(each => textOf(each) === step.see)
        : (step.inAlert === true ? [...main.querySelectorAll('[role="alert"]')] : [main]).some(
            each => [...each.querySelectorAll('*')].some(node => ownText(node) === step.see),
          );
    await until(`"${step.see}"`, found);
  }
}

/** Takes the steps in turn, from the screen just opened. */
export async function reach(shell: OpenShell, steps: readonly Step[]): Promise<void> {
  for (const step of steps) await take(shell, step);
}
