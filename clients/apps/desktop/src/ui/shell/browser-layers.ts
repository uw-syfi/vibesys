/**
 * The window's own implementations of the roles the appearance and the navigation read: the page
 * the theme is shown on, the system's `prefers-color-scheme`, and the keyboard. They live here, in
 * the `dom` runtime, because they touch the document; `core` holds the roles and their logic.
 */
import {applyTheme} from '@vibesys/ui';
import type {ThemeMode} from '@vibesys/ui/tokens';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Queue from 'effect/Queue';
import * as Stream from 'effect/Stream';
import {KeyPresses, SystemAppearance, ThemeDocument} from '../../core/index.js';
import type {KeyPress} from '../../state/index.js';

/** The theme goes on the document's root element, which is where the token blocks select it. */
export const ThemeDocumentLive: Layer.Layer<ThemeDocument> = Layer.succeed(
  ThemeDocument,
  ThemeDocument.of({show: theme => Effect.sync(() => applyTheme(theme))}),
);

const DARK = '(prefers-color-scheme: dark)';
const modeOf = (dark: boolean): ThemeMode => (dark ? 'dark' : 'light');

/** The window's `prefers-color-scheme`; a window with no media queries asks for light and never changes. */
export const SystemAppearanceFromWindow: Layer.Layer<SystemAppearance> = Layer.sync(
  SystemAppearance,
  () => {
    const query = globalThis.matchMedia?.(DARK);
    return SystemAppearance.of({
      current: Effect.sync(() => modeOf(query?.matches ?? false)),
      changes:
        query === undefined
          ? Stream.empty
          : Stream.callback<ThemeMode>(queue =>
              Effect.gen(function* () {
                const listen = (event: MediaQueryListEvent): void => {
                  Queue.offerUnsafe(queue, modeOf(event.matches));
                };
                query.addEventListener('change', listen);
                yield* Effect.addFinalizer(() =>
                  Effect.sync(() => query.removeEventListener('change', listen)),
                );
              }),
            ),
    });
  },
);

/**
 * Where Escape and the arrows already mean something else: a text field, a list of choices, an open
 * menu or a dialog. A radio or a checkbox is not one: Escape and Alt+Left mean back on them.
 */
const CAPTURING = [
  'textarea',
  'select',
  'input:not([type="radio"], [type="checkbox"], [type="button"], [type="submit"])',
  '[contenteditable]:not([contenteditable="false"])',
  '[role="textbox"], [role="combobox"], [role="listbox"], [role="menu"], [role="dialog"]',
  'dialog[open]',
].join(', ');

const capturing = (target: EventTarget | null): boolean =>
  target instanceof Element && target.closest(CAPTURING) !== null;

/** The window's keydown events, as plain facts: what was pressed, and whether anything else took it. */
export const KeyPressesFromWindow: Layer.Layer<KeyPresses> = Layer.succeed(
  KeyPresses,
  KeyPresses.of({
    presses: Stream.callback<KeyPress>(queue =>
      Effect.gen(function* () {
        const listen = (event: KeyboardEvent): void => {
          Queue.offerUnsafe(queue, {
            key: event.key,
            alt: event.altKey,
            ctrl: event.ctrlKey,
            meta: event.metaKey,
            shift: event.shiftKey,
            handled: event.defaultPrevented,
            capturing: capturing(event.target),
          });
        };
        window.addEventListener('keydown', listen);
        yield* Effect.addFinalizer(() =>
          Effect.sync(() => window.removeEventListener('keydown', listen)),
        );
      }),
    ),
  }),
);
