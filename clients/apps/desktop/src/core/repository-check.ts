/**
 * Asking the host whether a repository path is one a run could start in, while the path is typed.
 * The host's own start check answers (`CheckRepository`); this keeps its latest answer with the path
 * it was about and asks again only when typing has paused for `REPOSITORY_CHECK_DEBOUNCE`, so a
 * path being typed does not ask about every prefix. A question that was overtaken by a newer path
 * ends without asking, and an answer for a path that is no longer the latest is dropped.
 */
import * as Effect from 'effect/Effect';
import * as Ref from 'effect/Ref';
import * as Atom from 'effect/reactivity/Atom';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import type {RepositoryAnswer, RepositoryChecked} from '../state/index.js';
import type {HostParts} from './host-store.js';

/** How long typing must pause before the host is asked about the path. */
export const REPOSITORY_CHECK_DEBOUNCE = '300 millis';

export interface RepositoryCheck {
  /** The host's latest answer and the path it was about; undefined before any. */
  readonly state: Atom.Atom<RepositoryChecked | undefined>;
  /**
   * The path is now `repository`. Asks the host once typing pauses, unless a newer path arrived;
   * an empty path clears the answer. Never fails: a host that cannot say leaves the path unchecked.
   */
  readonly ask: (repository: string) => Effect.Effect<void>;
}

const UNASKED: RepositoryAnswer = {_tag: 'Unasked'};

export const makeRepositoryCheck = Effect.fn('RepositoryCheck.make')(function* ({
  client,
}: HostParts) {
  const latest = yield* Ref.make('');
  const checked = yield* SubscriptionRef.make<RepositoryChecked | undefined>(undefined);
  const answer = (repository: string): Effect.Effect<RepositoryAnswer> =>
    client.checkRepository({repository}).pipe(
      Effect.map(({refusal, tasks}): RepositoryAnswer => {
        if (refusal === undefined) return {_tag: 'Passed', tasks};
        // A host that cannot launch agents refuses whatever the path: that is not the path's fault.
        return refusal.notReady ? UNASKED : {_tag: 'Refused', reason: refusal.reason};
      }),
      Effect.orElseSucceed(() => UNASKED),
    );
  const ask = Effect.fn('RepositoryCheck.ask')(function* (typed: string) {
    const repository = typed.trim();
    yield* Ref.set(latest, repository);
    if (repository === '') return yield* SubscriptionRef.set(checked, undefined);
    yield* Effect.sleep(REPOSITORY_CHECK_DEBOUNCE);
    if ((yield* Ref.get(latest)) !== repository) return;
    const reply = yield* answer(repository);
    if ((yield* Ref.get(latest)) === repository)
      yield* SubscriptionRef.set(checked, {repository, answer: reply});
  });
  return {state: Atom.keepAlive(Atom.subscriptionRef(checked)), ask} satisfies RepositoryCheck;
});
