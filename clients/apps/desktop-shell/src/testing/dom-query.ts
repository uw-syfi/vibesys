/**
 * Finding what a person sees in the shell drawn in a DOM, by the words the browser journeys use:
 * a role and a name, a label, a text. A query matches as Playwright's `getByRole`, `getByLabel`
 * and `getByText` do (a string name is a case-insensitive part of the name, `exact` is the whole
 * name in its case, a pattern is tested), and `one` fails unless exactly one element matches, as
 * a strict locator does. A DOM has no stylesheet, so "shown" is what the markup says: an element
 * that is `hidden`, `aria-hidden` or `inert`, or inside one, is not drawn.
 */
import {act} from 'react';
import {hidden, nameOf, text} from './dom-controls.js';

/** What a role query asks for. */
export interface Query {
  readonly role: string;
  readonly name?: string | RegExp;
  readonly exact?: boolean;
  /** A heading's level. */
  readonly level?: number;
}

const TAGS: Readonly<Record<string, string>> = {
  button: 'button',
  select: 'combobox',
  textarea: 'textbox',
  nav: 'navigation',
  main: 'main',
  ul: 'list',
  ol: 'list',
  li: 'listitem',
  h1: 'heading',
  h2: 'heading',
  h3: 'heading',
  h4: 'heading',
  h5: 'heading',
  h6: 'heading',
  dialog: 'dialog',
  section: 'region',
  option: 'option',
  optgroup: 'group',
  table: 'table',
  tr: 'row',
  th: 'columnheader',
  td: 'cell',
};

const INPUTS: Readonly<Record<string, string>> = {
  radio: 'radio',
  checkbox: 'checkbox',
  range: 'slider',
  search: 'searchbox',
  button: 'button',
  submit: 'button',
};

/** The roles whose name is what they contain; the others are named by a label only. */
const NAMED_BY_CONTENT: ReadonlySet<string> = new Set([
  'button',
  'radio',
  'checkbox',
  'switch',
  'tab',
  'option',
  'menuitem',
  'treeitem',
  'heading',
  'link',
  'row',
  'cell',
  'columnheader',
]);

/** The role of `element`: its own `role`, else the one its tag (and type) gives. */
function roleOf(element: Element): string | null {
  const given = element.getAttribute('role');
  if (given !== null) return given;
  const tag = element.tagName.toLowerCase();
  if (tag === 'input') {
    return INPUTS[(element as HTMLInputElement).type] ?? 'textbox';
  }
  if (tag === 'a') return element.hasAttribute('href') ? 'link' : null;
  return TAGS[tag] ?? null;
}

/** The name `element` has as a `role`: a label's text, and for most roles what it contains. */
function nameFor(element: Element, role: string): string {
  const document = element.ownerDocument;
  if (
    NAMED_BY_CONTENT.has(role) ||
    element instanceof HTMLInputElement ||
    element instanceof HTMLSelectElement ||
    element instanceof HTMLTextAreaElement
  ) {
    return nameOf(element, document);
  }
  const label = element.getAttribute('aria-label');
  if (label !== null) return label.trim();
  const by = element.getAttribute('aria-labelledby');
  if (by === null) return '';
  return by
    .split(/\s+/)
    .map(id => text(document.getElementById(id)))
    .join(' ')
    .trim();
}

function matches(actual: string, wanted: string | RegExp | undefined, exact: boolean): boolean {
  if (wanted === undefined) return true;
  const clean = actual.replace(/\s+/g, ' ').trim();
  if (wanted instanceof RegExp) return wanted.test(clean);
  const asked = wanted.replace(/\s+/g, ' ').trim();
  return exact ? clean === asked : clean.toLowerCase().includes(asked.toLowerCase());
}

const describeQuery = (query: Query): string =>
  `${query.role}${query.name === undefined ? '' : ` ${String(query.name)}`}${query.exact === true ? ' (exact)' : ''}${query.level === undefined ? '' : ` level ${query.level}`}`;

/** Every element inside `scope` that is drawn and has the role (and name) asked for, in page order. */
export function all(scope: ParentNode, query: Query): Element[] {
  return [...scope.querySelectorAll('*')].filter(element => {
    if (hidden(element) || roleOf(element) !== query.role) return false;
    if (query.level !== undefined && element.tagName.toLowerCase() !== `h${query.level}`) {
      if (element.getAttribute('aria-level') !== String(query.level)) return false;
    }
    return matches(nameFor(element, query.role), query.name, query.exact === true);
  });
}

/** The element the query names; fails unless there is exactly one. */
export function one(scope: ParentNode, query: Query): HTMLElement {
  const found = all(scope, query);
  if (found.length !== 1) {
    throw new Error(`${describeQuery(query)} matches ${found.length} elements, not one`);
  }
  return found[0] as HTMLElement;
}

/** The element the query names if it is there, else null; fails if more than one match. */
export function maybe(scope: ParentNode, query: Query): HTMLElement | null {
  const found = all(scope, query);
  if (found.length > 1) throw new Error(`${describeQuery(query)} matches ${found.length} elements`);
  return (found[0] as HTMLElement | undefined) ?? null;
}

/** The fields of `scope` that a label names (`getByLabel`). */
function byLabel(scope: ParentNode, label: string | RegExp, exact = false): HTMLElement[] {
  return [...scope.querySelectorAll('input, select, textarea, [role]')].filter(
    (element): element is HTMLElement =>
      element instanceof HTMLElement &&
      !hidden(element) &&
      matches(nameOf(element, element.ownerDocument), label, exact),
  );
}

/** The one field a label names. */
export function field(scope: ParentNode, label: string | RegExp, exact = false): HTMLElement {
  const found = byLabel(scope, label, exact);
  if (found.length !== 1) throw new Error(`label ${String(label)} names ${found.length} fields`);
  return found[0] as HTMLElement;
}

/** The smallest drawn elements whose text is `wanted` (`getByText`). */
export function byText(scope: ParentNode, wanted: string | RegExp, exact = false): Element[] {
  const holds = (element: Element): boolean => matches(text(element), wanted, exact);
  return [...scope.querySelectorAll('*')].filter(
    element =>
      !hidden(element) &&
      holds(element) &&
      ![...element.children].some(child => !hidden(child) && holds(child)),
  );
}

/** Whether `scope` draws a text that is `wanted`. */
const shows = (scope: ParentNode, wanted: string | RegExp, exact = false): boolean =>
  byText(scope, wanted, exact).length > 0;

/** The text `scope` draws, its parts apart. */
export const textOf = (scope: Element): string => text(scope);

/** What a person reads as the name of a control: its label, or what it holds. */
export const nameOfControl = (element: Element): string => nameOf(element, element.ownerDocument);

/** Whether a control is closed to use: `disabled`, or `aria-disabled`. */
export const closed = (element: Element): boolean =>
  element.hasAttribute('disabled') || element.getAttribute('aria-disabled') === 'true';

/** Whether a radio or checkbox is on. */
export const checked = (element: Element): boolean =>
  (element instanceof HTMLInputElement && element.checked) ||
  element.getAttribute('aria-checked') === 'true';

/** Picks the option of `select` whose value is `value`. */
export async function pickValue(select: HTMLSelectElement, value: string): Promise<void> {
  const option = [...select.options].find(each => each.value === value);
  if (option === undefined) throw new Error(`the select has no option with the value "${value}"`);
  await act(async () => {
    option.selected = true;
    const view = select.ownerDocument.defaultView;
    if (view === null) throw new Error('the screen is not in a window');
    select.dispatchEvent(new view.Event('change', {bubbles: true}));
  });
}

/** How many turns of the event loop a screen gets to show what a step led to. */
const TURNS = 64;

/** Gives what has arrived its turns to reach the stores and the page, until `held` is true. */
export async function until(what: string, held: () => boolean): Promise<void> {
  for (let turn = 0; turn < TURNS; turn += 1) {
    if (held()) return;
    await act(async () => Promise.resolve());
  }
  throw new Error(`${what} did not appear in ${TURNS} turns`);
}

/** Gives the screen a few turns to take in what was just done. */
export async function settle(): Promise<void> {
  for (let turn = 0; turn < 8; turn += 1) await act(async () => Promise.resolve());
}

/** Returns once `scope` draws `wanted`; fails with what it does draw. */
export async function see(scope: Element, wanted: string | RegExp, exact = false): Promise<void> {
  try {
    await until(`"${String(wanted)}"`, () => shows(scope, wanted, exact));
  } catch {
    throw new Error(`"${String(wanted)}" is not drawn; the screen reads: ${text(scope)}`);
  }
}

/** Fails if `scope` draws `wanted`. */
export function notSee(scope: Element, wanted: string | RegExp, exact = false): void {
  if (shows(scope, wanted, exact)) throw new Error(`"${String(wanted)}" is drawn`);
}

/** Every text `scope` draws and the element that draws it, as the raw-text check reads a screen. */
export function drawnTexts(scope: Element): {element: Element; text: string}[] {
  const found: {element: Element; text: string}[] = [];
  const walk = (node: Node): void => {
    if (node instanceof Element) {
      if (hidden(node)) return;
      for (const child of node.childNodes) walk(child);
      return;
    }
    const value = (node.textContent ?? '').replace(/\s+/g, ' ').trim();
    if (node.nodeType === 3 && value !== '' && node.parentElement !== null) {
      found.push({element: node.parentElement, text: value});
    }
  };
  walk(scope);
  return found;
}

/**
 * Puts a checkbox or radio in the state `on`, as Playwright's `check` and `uncheck` do: it presses
 * until the control reads as wanted, for the few turns a late reply may take to settle the state
 * the press was made against (a dialog whose draft arrives after it opens).
 */
export async function setChecked(element: Element, on: boolean): Promise<void> {
  for (let turn = 0; turn < TURNS; turn += 1) {
    if (checked(element) === on) {
      await settle();
      if (checked(element) === on) return;
    }
    await act(async () => (element as HTMLElement).click());
    await settle();
  }
  throw new Error(`the control did not become ${on ? 'checked' : 'unchecked'} in ${TURNS} presses`);
}
