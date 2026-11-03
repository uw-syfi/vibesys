/**
 * A run that failed while setting up, before trying any idea, on the Run screen: one statement where
 * the charts, time bar and faces would be, with the run's whole reason and Run again as the way on.
 * Resume, which would repeat the same setup, is not offered, the header gives none of the reason,
 * and the run's log is one press away. The shell is drawn in a DOM over the Fake gateway serving the
 * fixture's derived replay; the layout is the browser's.
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {UNSTARTED_WORDS} from '@vibesys/feature-run-page/state';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {press} from './testing/dom-controls.js';
import {all, maybe, one, see, textOf, until} from './testing/dom-query.js';
import {fixtureWorld, shellOnRun} from './testing/run-screen.js';
import type {OpenShell} from './testing/shell-world.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'indexedDB', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

let shell: OpenShell | undefined;
afterEach(async () => {
  await shell?.close();
  shell = undefined;
});

const FIXTURE = fixtureNamed('failed-at-setup');
const REASON = FIXTURE.end?.reason ?? '';

/** How often the page says `words`, in its text and in the hints and descriptions it carries. */
function said(scope: Element, words: string): number {
  const hints = [...scope.querySelectorAll('[title], [aria-description]')].flatMap(each => [
    each.getAttribute('title') ?? '',
    each.getAttribute('aria-description') ?? '',
  ]);
  return [textOf(scope), ...hints].reduce((sum, text) => sum + text.split(words).length - 1, 0);
}

const buttons = (scope: Element, name: string) =>
  all(scope, {role: 'button', name, exact: true}).length;

test('a run that failed while setting up says so once, whole, with Run again and no Resume', async () => {
  shell = await shellOnRun(fixtureWorld(FIXTURE), new FakeClock().schedule, FIXTURE);
  const main = one(shell.container, {role: 'main'});
  await until(
    'the statement',
    () => maybe(main, {role: 'heading', name: UNSTARTED_WORDS.title, exact: true}) !== null,
  );
  expect(said(main, REASON)).toBe(1);
  expect(buttons(main, 'Resume')).toBe(0);
  expect(buttons(main, UNSTARTED_WORDS.runAgain)).toBe(1);
  // Nothing of an empty run: no time bar, figures, chart or faces.
  expect(maybe(main, {role: 'slider', name: 'Run position'})).toBeNull();
  expect(maybe(main, {role: 'radiogroup', name: 'Show'})).toBeNull();
  expect(main.querySelector('.vs-chart-box')).toBeNull();
  expect(textOf(main)).not.toContain('running time');

  // Its log is a press away; Run again then sits with the header's actions.
  await press(one(main, {role: 'button', name: UNSTARTED_WORDS.viewLogs, exact: true}));
  await see(main, `Run failed: ${REASON}`);
  expect(maybe(main, {role: 'heading', name: UNSTARTED_WORDS.title, exact: true})).toBeNull();
  expect(buttons(main, 'Resume')).toBe(0);
  expect(buttons(main, UNSTARTED_WORDS.runAgain)).toBe(1);
  one(main, {role: 'radiogroup', name: 'Show'});
});
