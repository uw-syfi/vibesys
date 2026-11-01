/**
 * The every-control-acts audit at the DOM tier: each control of a screen, found by role, is used
 * once on a fresh copy of the screen and must change what the screen shows. A tab that marks itself
 * selected while the panes stay put, a button wired to nothing, a field that drops what is typed:
 * each leaves what a person reads as it was, so each fails here by name.
 *
 * A control is a button, tab, radio, option, checkbox, switch, menu item, text field, select or
 * slider that is not hidden and not disabled, named as its label says it. A radio, tab or option
 * that is already the chosen one is left out, as choosing it again is meant to change nothing, and
 * the options of a select are the select's to pick from. What changed is read as a person reads the
 * screen: its text, the roles and names of what it draws, the values of its fields, and the states
 * that are not the control's own (checked, selected, pressed, current and focus).
 */
import {act} from 'react';

/** The roles the audit uses, and how it uses each. */
type ControlRole =
  | 'button'
  | 'tab'
  | 'radio'
  | 'option'
  | 'checkbox'
  | 'switch'
  | 'menuitem'
  | 'searchbox'
  | 'textbox'
  | 'combobox'
  | 'slider';

/** A control, found again by its role and name: the `nth` of those that share both. */
export interface Control {
  readonly role: ControlRole;
  readonly name: string;
  readonly nth: number;
}

export function describeControl(control: Control): string {
  return `${control.role} "${control.name}"${control.nth === 0 ? '' : ` (#${control.nth + 1})`}`;
}

const CANDIDATES = [
  'button',
  'input',
  'select',
  'textarea',
  '[role="button"]',
  '[role="tab"]',
  '[role="radio"]',
  '[role="option"]',
  '[role="checkbox"]',
  '[role="switch"]',
  '[role="menuitem"]',
  '[role="searchbox"]',
  '[role="textbox"]',
  '[role="combobox"]',
  '[role="slider"]',
].join(',');

const INPUT_ROLES: Readonly<Record<string, ControlRole>> = {
  radio: 'radio',
  checkbox: 'checkbox',
  range: 'slider',
  search: 'searchbox',
  text: 'textbox',
  email: 'textbox',
  url: 'textbox',
  number: 'textbox',
  password: 'textbox',
  '': 'textbox',
};

const ROLES: readonly ControlRole[] = [
  'button',
  'tab',
  'radio',
  'option',
  'checkbox',
  'switch',
  'menuitem',
  'searchbox',
  'textbox',
  'combobox',
  'slider',
];

/** The role of `element`: its own `role`, else the one its tag and type give. */
function roleOf(element: Element): ControlRole | null {
  const given = element.getAttribute('role');
  if (given !== null) return ROLES.find(role => role === given) ?? null;
  if (element instanceof HTMLButtonElement) return 'button';
  if (element instanceof HTMLSelectElement) return 'combobox';
  if (element instanceof HTMLTextAreaElement) return 'textbox';
  if (element instanceof HTMLInputElement) return INPUT_ROLES[element.type] ?? null;
  return null;
}

function hidden(element: Element): boolean {
  return element.closest('[hidden], [aria-hidden="true"], [inert]') !== null;
}

function text(element: Element | null): string {
  return (element?.textContent ?? '').replace(/\s+/g, ' ').trim();
}

/** The text of the `label` that names a field: the one for its id, else the one around it. */
function labelOf(element: Element, root: Document): string {
  if (!(element instanceof HTMLInputElement || element instanceof HTMLSelectElement)) return '';
  if (
    element instanceof HTMLInputElement &&
    (element.type === 'button' || element.type === 'submit')
  )
    return '';
  const named =
    element.id === ''
      ? undefined
      : [...root.querySelectorAll('label')].find(label => label.getAttribute('for') === element.id);
  return contentOf(named ?? element.closest('label'));
}

/**
 * What an element is named by its content: its text, each part standing apart from the next, and
 * a part that names itself (an icon, a marker) by that name instead of what is inside it.
 */
function contentOf(element: Element | null): string {
  if (element === null) return '';
  const parts: string[] = [];
  const walk = (node: Node): void => {
    if (node.nodeType === 3) parts.push(node.textContent ?? '');
    else if (node instanceof Element && !hidden(node) && !(node instanceof HTMLSelectElement)) {
      const label = node.getAttribute('aria-label');
      if (label === null) for (const child of node.childNodes) walk(child);
      else parts.push(label);
      parts.push(' ');
    }
  };
  for (const child of element.childNodes) walk(child);
  return parts.join(' ').replace(/\s+/g, ' ').trim();
}

/** The name a person reads for `element`: its label, what labels it, else the text inside it. */
function nameOf(element: Element, root: Document): string {
  const label = element.getAttribute('aria-label');
  if (label !== null) return label.trim();
  const labelledBy = element.getAttribute('aria-labelledby');
  if (labelledBy !== null) {
    return labelledBy
      .split(/\s+/)
      .map(id => contentOf(root.getElementById(id)))
      .join(' ')
      .trim();
  }
  const labelled = labelOf(element, root);
  if (labelled !== '') return labelled;
  if (element instanceof HTMLInputElement || element instanceof HTMLSelectElement) {
    return element.getAttribute('placeholder') ?? element.getAttribute('title') ?? '';
  }
  return contentOf(element) || (element.getAttribute('title') ?? '');
}

function disabled(element: Element): boolean {
  return (
    element.hasAttribute('disabled') ||
    element.getAttribute('aria-disabled') === 'true' ||
    element.closest('fieldset[disabled]') !== null
  );
}

function chosen(element: Element, role: ControlRole): boolean {
  if (role !== 'radio' && role !== 'tab' && role !== 'option') return false;
  if (element instanceof HTMLInputElement && element.checked) return true;
  return (
    element.getAttribute('aria-checked') === 'true' ||
    element.getAttribute('aria-selected') === 'true'
  );
}

/**
 * The enabled, named controls inside `scope`, in page order, each with the `nth` among those that
 * share its role and name (counted as `use` counts them, disabled or not).
 */
export function controlsOf(scope: Element): Control[] {
  const found: Control[] = [];
  const seen = new Map<string, number>();
  for (const element of scope.querySelectorAll(CANDIDATES)) {
    const role = roleOf(element);
    if (role === null || hidden(element) || element.parentElement?.closest('select')) continue;
    if (element instanceof HTMLInputElement && element.type === 'hidden') continue;
    const name = nameOf(element, scope.ownerDocument);
    if (name === '' && role !== 'textbox' && role !== 'searchbox') continue;
    const key = `${role} ${name}`;
    const nth = seen.get(key) ?? 0;
    seen.set(key, nth + 1);
    if (disabled(element) || chosen(element, role)) continue;
    found.push({role, name, nth});
  }
  return found;
}

/** The element of the `nth` control of `role` named `name` inside `scope`. */
export function find(scope: Element, control: Control): Element {
  let index = 0;
  for (const element of scope.querySelectorAll(CANDIDATES)) {
    if (roleOf(element) !== control.role || hidden(element)) continue;
    if (nameOf(element, scope.ownerDocument) !== control.name) continue;
    if (index === control.nth) return element;
    index += 1;
  }
  throw new Error(`${describeControl(control)} is not on the screen`);
}

/** An event of the element's own window: the DOM refuses one made by another realm's `Event`. */
function eventOf(element: Element, type: string): Event {
  const view = element.ownerDocument.defaultView;
  if (view === null) throw new Error('the screen is not in a window');
  return new view.Event(type, {bubbles: true});
}

/**
 * Writes `value` into a field as typing does, so a controlled field's handler sees it. Under
 * happy-dom React takes a text field's change from `keyup` on the focused field, not from `input`.
 */
function write(element: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const prototype = Object.getPrototypeOf(element) as object;
  const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
  element.focus();
  setter?.call(element, value);
  for (const type of ['input', 'change', 'keyup']) element.dispatchEvent(eventOf(element, type));
}

/** Clicks `element`; a mark drawn in SVG has no `click()`, so it gets the event a pointer sends. */
function click(element: Element): void {
  if (element instanceof HTMLElement) {
    element.click();
    return;
  }
  const view = element.ownerDocument.defaultView;
  if (view === null) throw new Error('the screen is not in a window');
  element.dispatchEvent(new view.MouseEvent('click', {bubbles: true, cancelable: true}));
}

/** Presses `element`. */
export async function press(element: Element): Promise<void> {
  await act(async () => click(element));
}

/** Types `text` into a field. */
export async function fillIn(
  element: HTMLInputElement | HTMLTextAreaElement,
  text: string,
): Promise<void> {
  await act(async () => write(element, text));
}

/** Picks the option of `select` that reads `label`. */
export async function pickLabel(select: HTMLSelectElement, label: string): Promise<void> {
  const option = [...select.options].find(each => text(each) === label);
  if (option === undefined) throw new Error(`the select has no option "${label}"`);
  await act(async () => {
    option.selected = true;
    select.dispatchEvent(eventOf(select, 'change'));
  });
}

/** Uses `control` as a person would: presses it, types in it, picks another option, or moves it. */
export async function use(scope: Element, control: Control): Promise<void> {
  const element = find(scope, control);
  await act(async () => {
    if (element instanceof HTMLSelectElement) {
      const other = [...element.options].find(option => option.value !== element.value);
      if (other === undefined) {
        throw new Error(`${describeControl(control)} has nothing else to pick`);
      }
      other.selected = true;
      element.dispatchEvent(eventOf(element, 'change'));
    } else if (control.role === 'slider' && element instanceof HTMLInputElement) {
      write(element, element.min === '' ? '0' : element.min);
    } else if (control.role === 'textbox' || control.role === 'searchbox') {
      write(element as HTMLInputElement | HTMLTextAreaElement, 'no idea reads like this');
    } else {
      click(element);
    }
  });
}

const KEPT_ATTRIBUTES = [
  'role',
  'aria-label',
  'aria-expanded',
  'aria-valuenow',
  'aria-valuetext',
  'aria-invalid',
  'aria-disabled',
  'disabled',
  'title',
  'placeholder',
  'href',
];

/** The part of an element a person reads that is not its text: what it is, its fields' values. */
function describe(node: Element): string[] {
  const attributes = KEPT_ATTRIBUTES.filter(name => node.hasAttribute(name))
    .map(name => `${name}=${node.getAttribute(name)}`)
    .join(' ');
  const lines = [`<${node.tagName.toLowerCase()} ${attributes}>`];
  if (node instanceof HTMLSelectElement) {
    lines.push(
      [...node.options]
        .filter(option => option.selected)
        .map(option => text(option))
        .join('|'),
    );
  } else if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
    if (node.type !== 'radio' && node.type !== 'checkbox') lines.push(`value=${node.value}`);
  }
  return lines;
}

/**
 * What a person reads off `root`: its text, the role and name of what it draws, the values of its
 * fields and the text each select shows, without the states that are the controls' own.
 */
export function readScreen(root: Element): string {
  const out: string[] = [];
  const walk = (node: Node): void => {
    if (node instanceof Element) {
      if (hidden(node)) return;
      out.push(...describe(node));
    } else {
      const value = (node.textContent ?? '').replace(/\s+/g, ' ').trim();
      if (node.nodeType === 3 && value !== '') out.push(value);
    }
    for (const child of node.childNodes) walk(child);
  };
  walk(root);
  return out.join('\n');
}
