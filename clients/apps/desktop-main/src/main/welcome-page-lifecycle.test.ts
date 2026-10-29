import {describe, expect, test} from 'bun:test';
import {observeWelcomePage, WelcomePageLifecycle} from './welcome-page-lifecycle.js';

type LoadOutcome = 'loaded' | 'rejected' | 'threw';

class FakeContents {
  readonly loaded: string[] = [];
  listenersAtLoad: string[] = [];
  readonly #outcome: LoadOutcome;
  readonly #listeners = new Map<string, Array<() => void>>();

  constructor(outcome: LoadOutcome = 'loaded') {
    this.#outcome = outcome;
  }

  once(event: 'render-process-gone' | 'destroyed', listener: () => void): void {
    this.#listeners.set(event, [...(this.#listeners.get(event) ?? []), listener]);
  }

  loadURL(url: string): Promise<void> {
    this.listenersAtLoad = [...this.#listeners.keys()].sort();
    this.loaded.push(url);
    if (this.#outcome === 'threw') throw new Error('load threw');
    if (this.#outcome === 'rejected') return Promise.reject(new Error('load rejected'));
    return Promise.resolve();
  }

  emit(event: 'render-process-gone' | 'destroyed'): void {
    const listeners = this.#listeners.get(event) ?? [];
    this.#listeners.delete(event);
    for (const listener of listeners) listener();
  }
}

interface Page {
  readonly id: string;
  readonly contents: FakeContents;
}

function page(id: string, outcome: LoadOutcome = 'loaded'): Page {
  return {id, contents: new FakeContents(outcome)};
}

function harness(replacementOutcome: LoadOutcome = 'loaded', recreationFails = false) {
  const events: string[] = [];
  const replacements: Page[] = [];
  let next = 1;
  const pages = new WelcomePageLifecycle<Page>({
    recreate: () => {
      if (recreationFails) {
        events.push('create:failed');
        throw new Error('could not create a replacement');
      }
      const replacement = page(`replacement-${next}`, replacementOutcome);
      next += 1;
      replacements.push(replacement);
      events.push(`create:${replacement.id}`);
      return replacement;
    },
    activate: (current, lost) => {
      events.push(`activate:${current.id}`);
      observeWelcomePage(current.contents, 'app://vibesys/welcome.html', lost);
    },
    retire: current => events.push(`retire:${current.id}`),
    documentRetired: () => events.push('document-retired'),
    recovering: reason => events.push(`recovering:${reason}`),
    fatal: reason => events.push(`fatal:${reason}`),
  });
  return {pages, events, replacements};
}

function onlyReplacement(replacements: readonly Page[]): Page {
  const replacement = replacements[0];
  if (replacement === undefined || replacements.length !== 1) {
    throw new Error(`expected one replacement, got ${replacements.length}`);
  }
  return replacement;
}

async function spin(): Promise<void> {
  for (let turn = 0; turn < 4; turn += 1) await Promise.resolve();
}

describe('WelcomePageLifecycle', () => {
  test('an initial load rejection is observed before load and replaced once', async () => {
    const {pages, events, replacements} = harness();
    const initial = page('initial', 'rejected');

    pages.start(initial);
    expect(initial.contents.listenersAtLoad).toEqual(['destroyed', 'render-process-gone']);
    await spin();

    expect(pages.current).toBe(onlyReplacement(replacements));
    expect(replacements).toHaveLength(1);
    expect(events).toEqual([
      'activate:initial',
      'retire:initial',
      'document-retired',
      'recovering:load-failed',
      'create:replacement-1',
      'activate:replacement-1',
    ]);
  });

  test('renderer-gone and destroyed reports retire and recover the current page exactly once', () => {
    const {pages, events, replacements} = harness();
    const initial = page('initial');
    pages.start(initial);

    initial.contents.emit('render-process-gone');
    initial.contents.emit('destroyed');

    expect(pages.current).toBe(onlyReplacement(replacements));
    expect(replacements).toHaveLength(1);
    expect(events.filter(event => event === 'retire:initial')).toHaveLength(1);
    expect(events.filter(event => event.startsWith('recovering:'))).toEqual([
      'recovering:render-process-gone',
    ]);
    expect(events.some(event => event.startsWith('fatal:'))).toBe(false);
  });

  test('callbacks from the retired page cannot revoke its successor', () => {
    const {pages, events, replacements} = harness();
    const initial = page('initial');
    pages.start(initial);
    initial.contents.emit('destroyed');
    const replacement = onlyReplacement(replacements);

    initial.contents.emit('render-process-gone');
    expect(pages.lost(initial, 'load-failed')).toBe('ignored');

    expect(pages.current).toBe(replacement);
    expect(replacements).toHaveLength(1);
    expect(events.filter(event => event.startsWith('fatal:'))).toEqual([]);
  });

  test('loss of the one replacement enters fatal shutdown exactly once', () => {
    const {pages, events, replacements} = harness();
    const initial = page('initial');
    pages.start(initial);
    initial.contents.emit('render-process-gone');
    const replacement = onlyReplacement(replacements);

    replacement.contents.emit('destroyed');
    replacement.contents.emit('render-process-gone');

    expect(pages.current).toBeNull();
    expect(replacements).toHaveLength(1);
    expect(events.filter(event => event === `retire:${replacement.id}`)).toHaveLength(1);
    expect(events.filter(event => event === 'document-retired')).toHaveLength(2);
    expect(events.filter(event => event.startsWith('fatal:'))).toEqual(['fatal:destroyed']);
  });

  test('a rejected replacement load exhausts recovery without leaving a current page', async () => {
    const {pages, events, replacements} = harness('rejected');
    const initial = page('initial', 'rejected');
    pages.start(initial);

    await spin();
    await spin();

    expect(pages.current).toBeNull();
    expect(replacements).toHaveLength(1);
    expect(events.filter(event => event.startsWith('fatal:'))).toEqual(['fatal:load-failed']);
  });

  test('failure to construct the one replacement enters fatal shutdown', () => {
    const {pages, events} = harness('loaded', true);
    const initial = page('initial');
    pages.start(initial);

    initial.contents.emit('render-process-gone');

    expect(pages.current).toBeNull();
    expect(events).toEqual([
      'activate:initial',
      'retire:initial',
      'document-retired',
      'recovering:render-process-gone',
      'create:failed',
      'fatal:recreation-failed',
    ]);
  });

  test('native-window abandonment closes the current page and fences later loss', () => {
    const {pages, events, replacements} = harness();
    const initial = page('initial');
    pages.start(initial);

    pages.abandon();
    pages.abandon();
    initial.contents.emit('destroyed');

    expect(pages.current).toBeNull();
    expect(replacements).toEqual([]);
    expect(events).toEqual(['activate:initial', 'retire:initial']);
  });

  test('native-window abandonment retires a recovered page exactly once', () => {
    const {pages, events, replacements} = harness();
    const initial = page('initial');
    pages.start(initial);
    initial.contents.emit('destroyed');
    const replacement = onlyReplacement(replacements);

    pages.abandon();
    pages.abandon();
    replacement.contents.emit('destroyed');

    expect(events.filter(event => event === `retire:${replacement.id}`)).toHaveLength(1);
    expect(events.filter(event => event.startsWith('fatal:'))).toEqual([]);
  });

  test('a synchronous initial load failure follows the same bounded recovery policy', () => {
    const {pages, events, replacements} = harness();
    pages.start(page('initial', 'threw'));

    expect(pages.current).toBe(onlyReplacement(replacements));
    expect(events.filter(event => event.startsWith('recovering:'))).toEqual([
      'recovering:load-failed',
    ]);
  });

  test('an initial activation exception fences, retires, and revokes before startup handles it', () => {
    const events: string[] = [];
    const initial = page('initial');
    const pages = new WelcomePageLifecycle<Page>({
      recreate: () => page('unused'),
      activate: () => {
        events.push('activate:initial');
        throw new Error('could not activate the initial page');
      },
      retire: current => events.push(`retire:${current.id}`),
      documentRetired: () => events.push('document-retired'),
      recovering: reason => events.push(`recovering:${reason}`),
      fatal: reason => events.push(`fatal:${reason}`),
    });

    expect(() => pages.start(initial)).toThrow('could not activate the initial page');
    expect(pages.current).toBeNull();
    expect(pages.lost(initial, 'destroyed')).toBe('ignored');
    pages.abandon();
    expect(events).toEqual(['activate:initial', 'retire:initial', 'document-retired']);
  });

  test('a synchronous loss owns recovery when initial activation subsequently throws', () => {
    const events: string[] = [];
    const initial = page('initial');
    const replacement = page('replacement');
    const pages = new WelcomePageLifecycle<Page>({
      recreate: () => replacement,
      activate: (current, lost) => {
        events.push(`activate:${current.id}`);
        if (current === initial) {
          lost('load-failed');
          throw new Error('stale activation failure');
        }
      },
      retire: current => events.push(`retire:${current.id}`),
      documentRetired: () => events.push('document-retired'),
      recovering: reason => events.push(`recovering:${reason}`),
      fatal: reason => events.push(`fatal:${reason}`),
    });

    expect(() => pages.start(initial)).not.toThrow();
    expect(pages.current).toBe(replacement);
    expect(events).toEqual([
      'activate:initial',
      'retire:initial',
      'document-retired',
      'recovering:load-failed',
      'activate:replacement',
    ]);
  });

  test('a synchronously lost replacement is retired once when its activation then throws', () => {
    const events: string[] = [];
    const initial = page('initial');
    const replacement = page('replacement');
    const pages = new WelcomePageLifecycle<Page>({
      recreate: () => replacement,
      activate: (current, lost) => {
        events.push(`activate:${current.id}`);
        if (current === replacement) {
          lost('load-failed');
          throw new Error('stale replacement activation failure');
        }
      },
      retire: current => events.push(`retire:${current.id}`),
      documentRetired: () => events.push('document-retired'),
      recovering: reason => events.push(`recovering:${reason}`),
      fatal: reason => events.push(`fatal:${reason}`),
    });

    pages.start(initial);

    expect(pages.lost(initial, 'destroyed')).toBe('fatal');
    expect(pages.current).toBeNull();
    expect(events.filter(event => event === 'retire:replacement')).toHaveLength(1);
    expect(events).toEqual([
      'activate:initial',
      'retire:initial',
      'document-retired',
      'recovering:destroyed',
      'activate:replacement',
      'retire:replacement',
      'document-retired',
      'fatal:load-failed',
    ]);
  });
});
