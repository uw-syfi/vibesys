/**
 * Where the person is, held once for the window: the navigation fold, moved by the screens' intents
 * and by the keys that mean back. Built once per window in a `Scope`; its key listener ends with it.
 */
import * as Context from 'effect/Context';
import * as Effect from 'effect/Effect';
import * as Atom from 'effect/reactivity/Atom';
import type * as Scope from 'effect/Scope';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {
  initialNavigation,
  isBack,
  type KeyPress,
  type Navigation,
  type NavigationAction,
  navigationReducer,
} from '../../state/index.js';

/** The key presses of the window, as plain facts a fold can judge. */
export class KeyPresses extends Context.Service<
  KeyPresses,
  {readonly presses: Stream.Stream<KeyPress>}
>()('app/KeyPresses') {}

export interface NavigationStore {
  readonly state: Atom.Atom<Navigation>;
  readonly go: (action: NavigationAction) => Effect.Effect<void>;
}

export const makeNavigation = Effect.fn('Navigation.make')(function* (): Effect.fn.Return<
  NavigationStore,
  never,
  KeyPresses | Scope.Scope
> {
  const keys = yield* KeyPresses;
  const ref = yield* SubscriptionRef.make(initialNavigation);
  const go = (action: NavigationAction) =>
    SubscriptionRef.update(ref, navigation => navigationReducer(navigation, action));
  yield* keys.presses.pipe(
    Stream.filter(isBack),
    Stream.runForEach(() => go({type: 'back'})),
    Effect.forkScoped,
  );
  return {state: Atom.keepAlive(Atom.subscriptionRef(ref)), go};
});
