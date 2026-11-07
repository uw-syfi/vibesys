/**
 * The appearance of the whole window: what the person saved, what the system asks for, and the
 * theme the page shows as a result. Built once per window in a `Scope`: it reads what was saved,
 * shows the theme before anything draws, then keeps showing it as either input changes.
 */

import type {Appearance} from '@vibesys/ui/tokens';
import * as Effect from 'effect/Effect';
import * as Atom from 'effect/reactivity/Atom';
import type * as Scope from 'effect/Scope';
import * as Semaphore from 'effect/Semaphore';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {
  type AppearanceAction,
  type AppearanceState,
  appearanceReducer,
  initialAppearance,
  themeShown,
} from '../../state/index.js';
import {Preferences} from './preferences.js';
import {SystemAppearance} from './system-appearance.js';
import {ThemeDocument} from './theme-document.js';

export interface AppearanceStore {
  /** What screens read: the choice, the system's mode and whether the choice is saved. */
  readonly state: Atom.Atom<AppearanceState>;
  /**
   * Shows `appearance` at once and saves it. A save that fails leaves it shown and says so in
   * `state`; of several quick choices the last is the one saved.
   */
  readonly choose: (appearance: Appearance) => Effect.Effect<void>;
}

export const makeAppearance = Effect.fn('Appearance.make')(function* (): Effect.fn.Return<
  AppearanceStore,
  never,
  Preferences | SystemAppearance | ThemeDocument | Scope.Scope
> {
  const preferences = yield* Preferences;
  const system = yield* SystemAppearance;
  const page = yield* ThemeDocument;
  const ref = yield* SubscriptionRef.make(initialAppearance(yield* system.current));
  const apply = (action: AppearanceAction) =>
    SubscriptionRef.update(ref, state => appearanceReducer(state, action));

  const saved = yield* preferences.load.pipe(
    Effect.catch(error =>
      Effect.logWarning(`could not read the saved appearance: ${error.detail}`).pipe(
        Effect.as(null),
      ),
    ),
  );
  yield* apply({type: 'loaded', appearance: saved});
  // Before the first draw, so the page never shows a theme the person did not choose.
  yield* page.show(themeShown(yield* SubscriptionRef.get(ref)));
  yield* SubscriptionRef.changes(ref).pipe(
    Stream.map(themeShown),
    Stream.changes,
    Stream.runForEach(page.show),
    Effect.forkScoped,
  );
  yield* system.changes.pipe(
    Stream.runForEach(mode => apply({type: 'system', mode})),
    Effect.forkScoped,
  );

  const saving = yield* Semaphore.make(1);
  /** Saves what is chosen when its turn comes, so queued saves collapse to the latest choice. */
  const persist = saving.withPermits(1)(
    Effect.gen(function* () {
      const {appearance} = yield* SubscriptionRef.get(ref);
      yield* preferences.save(appearance).pipe(
        Effect.matchEffect({
          onSuccess: () => apply({type: 'saved'}),
          onFailure: () => apply({type: 'save-failed'}),
        }),
      );
    }),
  );
  const choose = Effect.fn('Appearance.choose')(function* (appearance: Appearance) {
    yield* apply({type: 'chosen', appearance});
    yield* persist;
  });
  return {state: Atom.keepAlive(Atom.subscriptionRef(ref)), choose};
});
