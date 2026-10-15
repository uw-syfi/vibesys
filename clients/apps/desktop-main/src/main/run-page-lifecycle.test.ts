import {describe, expect, test} from 'bun:test';
import {observeRunPage, RunPageLifecycle} from './run-page-lifecycle.js';

interface Page {
  readonly id: string;
}

function harness() {
  const events: string[] = [];
  const pages = new RunPageLifecycle<Page>({
    retire: page => events.push(`retire:${page.id}`),
    showWelcome: () => events.push('welcome'),
    currentPageLost: () => events.push('lost'),
  });
  return {pages, events};
}

describe('RunPageLifecycle', () => {
  test('a replaced page cannot revoke or detach its successor', () => {
    const {pages, events} = harness();
    const old = {id: 'old'};
    const current = {id: 'current'};

    pages.replace(old);
    pages.replace(current);
    expect(pages.current).toBe(current);
    expect(events).toEqual(['retire:old']);

    expect(pages.lost(old)).toBe(false);
    expect(pages.close(old)).toBe(false);
    expect(pages.current).toBe(current);
    expect(events).toEqual(['retire:old']);
  });

  test('unexpected current-page loss restores welcome and detaches exactly once', () => {
    const {pages, events} = harness();
    const page = {id: 'run'};
    pages.replace(page);

    expect(pages.lost(page)).toBe(true);
    expect(pages.current).toBeNull();
    expect(events).toEqual(['retire:run', 'welcome', 'lost']);

    expect(pages.lost(page)).toBe(false);
    expect(pages.close(page)).toBe(false);
    expect(events).toEqual(['retire:run', 'welcome', 'lost']);

    const replacement = {id: 'replacement'};
    pages.replace(replacement);
    expect(pages.lost(replacement)).toBe(true);
    expect(events).toEqual([
      'retire:run',
      'welcome',
      'lost',
      'retire:replacement',
      'welcome',
      'lost',
    ]);
  });

  test('deliberate close restores welcome without reporting binding loss', () => {
    const {pages, events} = harness();
    const page = {id: 'run'};
    pages.replace(page);

    expect(pages.close(page)).toBe(true);
    expect(pages.current).toBeNull();
    expect(events).toEqual(['retire:run', 'welcome']);
  });

  test('deliberate close restores welcome when page retirement fails', () => {
    const failure = new Error('view was already detached');
    const events: string[] = [];
    const pages = new RunPageLifecycle<Page>({
      retire: page => {
        events.push(`retire:${page.id}`);
        throw failure;
      },
      showWelcome: () => events.push('welcome'),
      currentPageLost: () => events.push('lost'),
    });
    const page = {id: 'run'};
    pages.replace(page);

    let thrown: unknown;
    try {
      pages.close(page);
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBe(failure);
    expect(pages.current).toBeNull();
    expect(events).toEqual(['retire:run', 'welcome']);
    expect(pages.close(page)).toBe(false);
  });

  test('unexpected loss attempts every terminal effect and preserves the first failure', () => {
    type Effect = 'retire' | 'welcome' | 'lost';
    const order: readonly Effect[] = ['retire', 'welcome', 'lost'];
    for (let failureMask = 1; failureMask < 1 << order.length; failureMask += 1) {
      const events: Effect[] = [];
      const failures = Object.fromEntries(
        order.map(effect => [effect, new Error(`${effect} failed`)]),
      ) as Record<Effect, Error>;
      const perform = (effect: Effect): void => {
        events.push(effect);
        const index = order.indexOf(effect);
        if ((failureMask & (1 << index)) !== 0) throw failures[effect];
      };
      const pages = new RunPageLifecycle<Page>({
        retire: () => perform('retire'),
        showWelcome: () => perform('welcome'),
        currentPageLost: () => perform('lost'),
      });
      const page = {id: `run-${failureMask}`};
      pages.replace(page);

      let thrown: unknown;
      try {
        pages.lost(page);
      } catch (error) {
        thrown = error;
      }

      const firstFailed = order.find((_effect, index) => (failureMask & (1 << index)) !== 0);
      if (firstFailed === undefined) throw new Error(`mask ${failureMask} selects no failure`);
      expect(events).toEqual([...order]);
      expect(thrown).toBe(failures[firstFailed]);
      expect(pages.current).toBeNull();
      expect(pages.lost(page)).toBe(false);
    }
  });

  test('native-window abandonment fences every later child callback', () => {
    const {pages, events} = harness();
    const page = {id: 'run'};
    pages.replace(page);
    pages.abandon();

    expect(pages.lost(page)).toBe(false);
    expect(pages.current).toBeNull();
    expect(events).toEqual(['retire:run']);
  });

  test('renderer loss wiring retires once even when destruction follows', async () => {
    const {pages, events} = harness();
    const page = {id: 'run'};
    const contents = new FakeRunPageContents();
    pages.replace(page);

    await observeRunPage(
      contents,
      'app://vibesys/index.html',
      () => pages.lost(page),
      () => events.push('load-failed'),
    );
    expect(contents.loaded).toEqual(['app://vibesys/index.html']);

    contents.emit('render-process-gone');
    contents.emit('destroyed');

    expect(events).toEqual(['retire:run', 'welcome', 'lost']);
  });

  test('load failure reports and retires the exact current page', async () => {
    const {pages, events} = harness();
    const page = {id: 'run'};
    const contents = new FakeRunPageContents(true);
    pages.replace(page);

    await observeRunPage(
      contents,
      'app://vibesys/index.html',
      () => pages.lost(page),
      () => events.push('load-failed'),
    );

    expect(events).toEqual(['load-failed', 'retire:run', 'welcome', 'lost']);
  });
});

class FakeRunPageContents {
  readonly loaded: string[] = [];
  readonly #listeners = new Map<string, Array<() => void>>();
  readonly #failLoad: boolean;

  constructor(failLoad = false) {
    this.#failLoad = failLoad;
  }

  once(event: 'render-process-gone' | 'destroyed', listener: () => void): void {
    this.#listeners.set(event, [...(this.#listeners.get(event) ?? []), listener]);
  }

  async loadURL(url: string): Promise<void> {
    this.loaded.push(url);
    if (this.#failLoad) throw new Error('renderer failed to load');
  }

  emit(event: 'render-process-gone' | 'destroyed'): void {
    const listeners = this.#listeners.get(event) ?? [];
    this.#listeners.delete(event);
    for (const listener of listeners) listener();
  }
}
