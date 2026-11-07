import {describe, expect, it} from '@effect/vitest';
import type {ThemeMode} from '@vibesys/ui/tokens';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as AtomRegistry from 'effect/reactivity/AtomRegistry';
import * as Stream from 'effect/Stream';
import {makeNavigation, SystemAppearance, ThemeDocument} from '../../core/index.js';
import {installDom} from '../../testing/dom/harness.js';
import {quiescent, settle} from '../../testing/settle.js';
import {
  KeyPressesFromWindow,
  SystemAppearanceFromWindow,
  ThemeDocumentLive,
} from './browser-layers.js';

const dom = installDom();

const keydown = (target: EventTarget, init: KeyboardEventInit): void => {
  target.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, cancelable: true, ...init}));
};

type MediaListener = (event: {readonly matches: boolean}) => void;

/** A `matchMedia` for the dark query that a test flips, and that says how many listeners it has. */
function installMatchMedia(dark: boolean) {
  const listeners = new Set<MediaListener>();
  let matches = dark;
  Object.assign(globalThis, {
    matchMedia: () => ({
      get matches() {
        return matches;
      },
      addEventListener: (_type: string, listener: MediaListener) => listeners.add(listener),
      removeEventListener: (_type: string, listener: MediaListener) => listeners.delete(listener),
    }),
  });
  return {
    set: (next: boolean) => {
      matches = next;
      for (const listener of listeners) listener({matches: next});
    },
    listeners: () => listeners.size,
  };
}

describe('the keyboard of the window', () => {
  it.effect('Escape and Alt+Left on the page go back, where no control uses them', () =>
    quiescent(
      Effect.gen(function* () {
        const store = yield* makeNavigation().pipe(Effect.provide(KeyPressesFromWindow));
        const registry = AtomRegistry.make();
        const destination = Effect.gen(function* () {
          yield* settle;
          return registry.get(store.state).destination;
        });
        const field = document.createElement('input');
        field.type = 'text';
        const radio = document.createElement('input');
        radio.type = 'radio';
        document.body.append(field, radio);
        yield* settle;
        yield* store.go({type: 'open-settings'});
        // A text field keeps Escape, a press without Alt is not back, and a handled press is not either.
        keydown(field, {key: 'Escape'});
        keydown(document.body, {key: 'ArrowLeft'});
        keydown(document.body, {key: 'Enter'});
        document.body.addEventListener('keydown', event => event.preventDefault(), {once: true});
        keydown(document.body, {key: 'Escape'});
        expect(yield* destination).toBe('settings');
        // A radio, which the settings use, does not: Escape and Alt+Left mean back there.
        keydown(radio, {key: 'Escape'});
        expect(yield* destination).toBe('runs');
        yield* store.go({type: 'open-settings'});
        keydown(document.body, {key: 'ArrowLeft', altKey: true});
        expect(yield* destination).toBe('runs');
      }),
    ),
  );
});

describe('the page the theme is shown on', () => {
  it.effect('shows the first theme as it is and every later one with motion', () =>
    Effect.gen(function* () {
      const root = dom.window().document.documentElement;
      const page = yield* ThemeDocument.pipe(Effect.provide(ThemeDocumentLive));
      yield* page.show('midnight');
      expect(root.getAttribute('data-theme')).toBe('midnight');
      expect(root.hasAttribute('data-theme-motion')).toBe(false);
      yield* page.show('sand');
      expect(root.getAttribute('data-theme')).toBe('sand');
      expect(root.hasAttribute('data-theme-motion')).toBe(true);
    }),
  );
});

describe('what the system asks for', () => {
  it.effect(
    'is read from the window and followed live, and the listener ends with the stream',
    () =>
      quiescent(
        Effect.gen(function* () {
          const media = installMatchMedia(true);
          const system = yield* SystemAppearance.pipe(Effect.provide(SystemAppearanceFromWindow));
          expect(yield* system.current).toBe('dark');
          const seen: ThemeMode[] = [];
          const fiber = yield* system.changes.pipe(
            Stream.take(2),
            Stream.runForEach(mode => Effect.sync(() => seen.push(mode))),
            Effect.forkChild,
          );
          yield* settle;
          expect(media.listeners()).toBe(1);
          media.set(false);
          media.set(true);
          yield* Fiber.join(fiber);
          expect(seen).toEqual(['light', 'dark']);
          yield* settle;
          expect(media.listeners()).toBe(0);
        }),
      ),
  );
});
