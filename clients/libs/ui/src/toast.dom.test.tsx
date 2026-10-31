/** Toasts and banners in a browser DOM, through the `.` entry only. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act, type ReactNode, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Banner, type BannerTone, Button, type ToastItem, ToastStack} from './index.js';

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

async function mount(node: ReactNode): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () => createRoot(container).render(node));
  return container;
}

async function click(element: Element): Promise<void> {
  await act(async () => (element as HTMLElement).click());
}

const buttons = (container: HTMLElement): HTMLButtonElement[] => [
  ...container.querySelectorAll('button'),
];
const named = (container: HTMLElement, name: string): HTMLButtonElement => {
  const found = buttons(container).find(
    button => (button.getAttribute('aria-label') ?? button.textContent) === name,
  );
  if (found === undefined) throw new Error(`no button named ${name}`);
  return found;
};

const SAVE: ToastItem = {
  id: 'save',
  tone: 'err',
  title: 'Could not save the change',
  detail: 'The settings file is read-only.',
  open: false,
};
const READ: ToastItem = {
  id: 'read',
  tone: 'warn',
  title: 'The machines could not be read',
  open: false,
};

/** What owns a stack: which toasts there are and which are open, the way the app's board does. */
function Owner(props: {readonly initial: readonly ToastItem[]}): ReactNode {
  const [toasts, setToasts] = useState(props.initial);
  return (
    <ToastStack
      label="Notifications"
      toasts={toasts}
      toggle={(id, open) =>
        setToasts(list => list.map(toast => (toast.id === id ? {...toast, open} : toast)))
      }
      dismiss={id => setToasts(list => list.filter(toast => toast.id !== id))}
    />
  );
}

describe('a toast', () => {
  test('is collapsed by default: a title, a tone word, an expand control and a dismiss', async () => {
    const container = await mount(<Owner initial={[SAVE]} />);
    expect(container.textContent).toContain('Could not save the change');
    expect(container.textContent).not.toContain('read-only');
    expect(named(container, 'Show details').getAttribute('aria-expanded')).toBe('false');
    expect(container.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe('Error');
    expect(named(container, 'Dismiss')).toBeDefined();
  });

  test('opens to its detail and closes again', async () => {
    const container = await mount(<Owner initial={[SAVE]} />);
    await click(named(container, 'Show details'));
    expect(container.textContent).toContain('The settings file is read-only.');
    expect(named(container, 'Hide details').getAttribute('aria-expanded')).toBe('true');
    await click(named(container, 'Hide details'));
    expect(container.textContent).not.toContain('read-only');
  });

  test('is dismissed by its button and leaves the others', async () => {
    const container = await mount(<Owner initial={[SAVE, READ]} />);
    const dismiss = buttons(container).filter(
      button => button.getAttribute('aria-label') === 'Dismiss',
    );
    expect(dismiss).toHaveLength(2);
    await click(dismiss[0] as HTMLButtonElement);
    expect(container.textContent).not.toContain('Could not save the change');
    expect(container.textContent).toContain('The machines could not be read');
  });

  test('without a detail has nothing to expand', async () => {
    const container = await mount(<Owner initial={[READ]} />);
    expect(buttons(container).map(button => button.getAttribute('aria-label'))).toEqual([
      'Dismiss',
    ]);
    expect(container.querySelector('[aria-expanded]')).toBeNull();
  });

  test('is never a control named like an action beside it: only its two buttons are controls', async () => {
    const container = await mount(<Owner initial={[SAVE]} />);
    expect(buttons(container).map(button => button.getAttribute('aria-label'))).toEqual([
      'Show details',
      'Dismiss',
    ]);
  });
});

describe('a stack of toasts', () => {
  test('is a labelled live region that is present while empty, so an added toast is announced', async () => {
    const container = await mount(<Owner initial={[]} />);
    const region = container.querySelector('section');
    expect(region?.getAttribute('aria-label')).toBe('Notifications');
    expect(region?.getAttribute('aria-live')).toBe('polite');
    expect(region?.children).toHaveLength(0);
  });

  test('announces an error at once and a warning when the reader is idle', async () => {
    const container = await mount(<Owner initial={[SAVE, READ]} />);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Could not save');
    expect(container.querySelector('[role="status"]')?.textContent).toContain('could not be read');
  });

  test('lists toasts in the order given and puts every control in the tab order', async () => {
    const container = await mount(<Owner initial={[READ, SAVE]} />);
    const order = buttons(container).map(button => button.getAttribute('aria-label'));
    expect(order).toEqual(['Dismiss', 'Show details', 'Dismiss']);
    expect(container.textContent?.indexOf('could not be read')).toBeLessThan(
      container.textContent?.indexOf('Could not save') ?? 0,
    );
    for (const button of buttons(container)) {
      expect(button.getAttribute('tabindex')).toBeNull();
      expect(button.disabled).toBe(false);
      expect(button.type).toBe('button');
    }
  });
});

describe('a banner', () => {
  test('is one line until its details control is opened to the body and hint behind it', async () => {
    const container = await mount(
      <Banner tone="warn" title="Needs an update" body="The body." hint="The hint." />,
    );
    expect(container.textContent).toContain('Needs an update');
    expect(container.textContent).not.toContain('The body.');
    await click(named(container, 'Show details'));
    expect(container.textContent).toContain('The body.');
    expect(container.textContent).toContain('The hint.');
  });

  test('keeps its actions reachable while closed, and its title is not a control', async () => {
    let pressed = 0;
    const container = await mount(
      <Banner
        tone="failed"
        title="Run again"
        body="Why it ended."
        actions={<Button text="Run again" press={() => (pressed += 1)} />}
      />,
    );
    expect(container.textContent).not.toContain('Why it ended.');
    await click(named(container, 'Run again'));
    expect(pressed).toBe(1);
    expect(buttons(container).filter(button => button.textContent === 'Run again')).toHaveLength(1);
  });

  test('without a body or hint has nothing to open', async () => {
    const container = await mount(<Banner title="No VibeSys checkout" />);
    expect(container.querySelector('button')).toBeNull();
    expect(container.textContent).toBe('No VibeSys checkout');
  });

  test('has no error tone: a failure passed through is a toast, a failure held is `failed`', () => {
    const rejectsErr: 'err' extends BannerTone ? never : true = true;
    const acceptsFailed: 'failed' extends BannerTone ? true : never = true;
    expect([rejectsErr, acceptsFailed]).toEqual([true, true]);
  });
});
