/**
 * Driving the guided New run drawn in a DOM, as the browser journeys do (`e2e/new-run-flow.ts`):
 * choosing an answer in a question's group, going on, and reading the question and the steps.
 */

import {press} from './dom-controls.js';
import {all, maybe, one, settle, textOf} from './dom-query.js';
import type {FakeNewRun} from './fake-bridge.js';
import type {OpenShell} from './shell-world.js';

export const main = (shell: OpenShell): HTMLElement => one(shell.container, {role: 'main'});

export const steps = (shell: OpenShell): HTMLElement =>
  one(shell.container, {role: 'navigation', name: 'New run steps'});

/**
 * The steps of the guided form, as the wide window lists them: the buttons that open a step, not
 * the disclosure that stands for the same list when the window is narrow.
 */
const stepButtons = (shell: OpenShell): Element[] =>
  all(steps(shell), {role: 'button'}).filter(each => !each.hasAttribute('aria-controls'));

/** The one step whose words hold `label` (the step's name, or the answer it shows). */
export function stepButton(shell: OpenShell, label: string | RegExp): HTMLElement {
  const holds = (text: string): boolean =>
    label instanceof RegExp ? label.test(text) : text.toLowerCase().includes(label.toLowerCase());
  const found = stepButtons(shell).filter(each => holds(textOf(each)));
  if (found.length !== 1) throw new Error(`${found.length} steps hold ${String(label)}`);
  return found[0] as HTMLElement;
}

/** The question the page asks, or null when another page is shown. */
export const question = (shell: OpenShell, title: string | RegExp): HTMLElement | null =>
  maybe(main(shell), {role: 'heading', level: 1, name: title});

/** Choose `name` in the question's group `group`. */
export async function choose(
  shell: OpenShell,
  group: string,
  name: string | RegExp,
): Promise<void> {
  const radios = one(main(shell), {role: 'radiogroup', name: group, exact: true});
  await press(one(radios, {role: 'radio', name}));
  await settle();
}

/** Press Continue (or `label`). */
export async function proceed(shell: OpenShell, label = 'Continue'): Promise<void> {
  await press(one(main(shell), {role: 'button', name: label, exact: true}));
  await settle();
}

/** What the Fake helper was asked to do. */
export const asked = (): FakeNewRun =>
  (globalThis.window as unknown as {fakeHost: {newRun: FakeNewRun}}).fakeHost.newRun;
