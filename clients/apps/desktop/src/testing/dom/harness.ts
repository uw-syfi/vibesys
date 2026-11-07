/**
 * A DOM for testing screens by what a person does: render a screen, find a control by its role and
 * accessible name, press it, and read what is on screen. The DOM is happy-dom's, installed as the
 * test's globals and removed after; nothing here reads a clock or waits.
 */
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterAll, beforeAll} from 'vitest';

/** Installs a fresh DOM as the globals for this file's tests, and removes it after them. */
export function installDom(): {readonly window: () => Window} {
  const window = new Window({url: 'http://localhost/'});
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
  afterAll(() => {
    for (const name of installed) Reflect.deleteProperty(globalThis, name);
    Reflect.deleteProperty(globalThis, 'window');
    Reflect.deleteProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT');
  });
  return {window: () => window};
}

const CONTROLS =
  'button, a, [role="tab"], [role="radio"], [role="treeitem"], [role="option"], tr[data-selectable]';

/** What names `element` for a person: its `aria-label`, else its text. */
const nameOf = (element: Element): string =>
  element.getAttribute('aria-label') ?? element.textContent?.trim() ?? '';

export interface Screen {
  readonly container: HTMLElement;
  /** Every control whose accessible name is exactly `name`. */
  readonly controls: (name: string) => HTMLElement[];
  /** The one control named `name`: none or several is an error naming what was there. */
  readonly control: (name: string) => HTMLElement;
  /** A press, as a person makes it. */
  readonly press: (element: HTMLElement) => void;
  /** The text a person can read on the screen now. */
  readonly text: () => string;
  /** Draws `element` again (a new state of the same screen). */
  readonly render: (element: ReactNode) => void;
  readonly unmount: () => void;
}

/** Draws `element` into a fresh container of the installed DOM. */
export function mount(element: ReactNode): Screen {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root: Root = createRoot(container);
  const render = (next: ReactNode): void => {
    act(() => root.render(next));
  };
  render(element);
  const controls = (name: string): HTMLElement[] =>
    [...container.querySelectorAll<HTMLElement>(CONTROLS)].filter(each => nameOf(each) === name);
  return {
    container,
    controls,
    control: name => {
      const found = controls(name);
      const [only] = found;
      if (found.length !== 1 || only === undefined) {
        const names = [...container.querySelectorAll(CONTROLS)].map(nameOf).join(' | ');
        throw new Error(
          `expected one control named ${JSON.stringify(name)}, found ${found.length} among: ${names}`,
        );
      }
      return only;
    },
    press: control => {
      act(() => {
        control.click();
      });
    },
    text: () => container.textContent ?? '',
    render,
    unmount: () => {
      act(() => root.unmount());
      container.remove();
    },
  };
}
