/**
 * The every-control-acts check: each control of the Run screen, used once, must change what the
 * screen shows. Controls are found by role from the page's accessibility tree (a tab, a toggle, a
 * row, a field), not listed by hand, so a control added later is checked without a new test. Each
 * is used on a fresh copy of the screen (`controlsOf` names them, `inertness` uses one), and the
 * screen after must differ from the one before in anything but the state of controls themselves
 * (which is checked, selected, pressed, current or focused): a tab that only marks itself selected
 * while the panes stay as they were is dead. The comparison reads the accessibility tree once the
 * screen has settled (`settle.ts`), so it never waits for a duration.
 */
import type {BrowserContext, Locator, Page} from '@playwright/test';
import {settled} from './settle.js';

/** The roles the check uses, and how it uses each. */
const CLICKED = ['button', 'tab', 'radio', 'option', 'checkbox', 'switch', 'menuitem'] as const;
const FIELDS = ['searchbox', 'textbox'] as const;
type Role = (typeof CLICKED)[number] | (typeof FIELDS)[number] | 'combobox' | 'slider';
const ROLES: readonly Role[] = [...CLICKED, ...FIELDS, 'combobox', 'slider'];

/** A control, found again by its role and name: the `nth` of those that share both. */
export interface Control {
  readonly role: Role;
  readonly name: string;
  readonly nth: number;
}

function describeControl(control: Control): string {
  return `${control.role} "${control.name}"${control.nth === 0 ? '' : ` (#${control.nth + 1})`}`;
}

const LINE = /^\s*- ([a-z]+) (?:"((?:[^"\\]|\\.)*)")?(.*)$/;

/**
 * The enabled, named controls inside `scope`, in page order. A radio, tab or row that is already
 * the chosen one is left out: choosing it again is meant to change nothing.
 */
export async function controlsOf(scope: Locator): Promise<Control[]> {
  const found: Control[] = [];
  const seen = new Map<string, number>();
  let insideSelect: number | null = null;
  for (const line of (await scope.ariaSnapshot()).split('\n')) {
    const match = LINE.exec(line);
    const depth = line.length - line.trimStart().length;
    // The options of a select are the select's to pick from, not controls of their own.
    if (insideSelect !== null && depth > insideSelect) continue;
    insideSelect = match?.[1] === 'combobox' ? depth : null;
    const role = ROLES.find(each => each === match?.[1]);
    const name = match?.[2];
    if (role === undefined || name === undefined) continue;
    const state = match?.[3] ?? '';
    const chosen =
      ['radio', 'tab', 'option'].includes(role) && /\[(?:checked|selected)\]/.test(state);
    const text = JSON.parse(`"${name}"`) as string;
    const key = `${role} ${text}`;
    // The index counts every control of that role and name, as the locator does, disabled or not.
    const nth = seen.get(key) ?? 0;
    seen.set(key, nth + 1);
    if (state.includes('[disabled]') || chosen) continue;
    found.push({role, name: text, nth});
  }
  return found;
}

/** What a person reads off the page: its accessibility tree without the controls' own state. */
async function content(page: Page): Promise<string> {
  const tree = await page.locator('body').ariaSnapshot();
  return tree.replace(/ \[(?:active|checked|selected|pressed|current)(?:=[^\]]*)?\]/g, '');
}

/** Uses `control` as a person would: presses it, types in it, picks another option, or moves it. */
async function use(scope: Locator, control: Control): Promise<void> {
  const target = scope.getByRole(control.role, {name: control.name, exact: true}).nth(control.nth);
  if (control.role === 'combobox') {
    const options = await target
      .locator('option')
      .evaluateAll(each => each.map(option => (option as HTMLOptionElement).value));
    const current = await target.inputValue();
    const other = options.find(value => value !== current);
    if (other === undefined)
      throw new Error(`${describeControl(control)} has nothing else to pick`);
    await target.selectOption(other);
  } else if (control.role === 'slider') {
    await target.focus();
    await target.page().keyboard.press('Home');
  } else if (FIELDS.some(role => role === control.role)) {
    await target.fill('no idea reads like this');
  } else {
    await target.click();
  }
}

/** `null` when using `control` changes the screen; else the sentence saying it does nothing. */
async function inertness(page: Page, scope: Locator, control: Control): Promise<string | null> {
  await settled(page);
  const before = await content(page);
  await use(scope, control);
  await settled(page);
  return (await content(page)) === before
    ? `${describeControl(control)} changes nothing on the screen`
    : null;
}

/** Fresh pages for the audit, with at most two renderers alive per worker. */
export async function checkControls(
  context: BrowserContext,
  controls: readonly Control[],
  reach: (page: Page) => Promise<void>,
  scope: (page: Page) => Locator,
): Promise<(string | null)[]> {
  const found: (string | null)[] = [];
  const pending = controls.entries();
  const outcomes = await Promise.allSettled(
    Array.from({length: Math.min(2, controls.length)}, async () => {
      for (const [index, control] of pending) {
        const page = await context.newPage();
        try {
          await reach(page);
          found[index] = await inertness(page, scope(page), control);
        } finally {
          await page.close();
        }
      }
    }),
  );
  for (const outcome of outcomes) {
    if (outcome.status === 'rejected') throw outcome.reason;
  }
  return found;
}
