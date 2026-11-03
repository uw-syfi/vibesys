/** Where ids come from, so a test fixes them with a seeded `Random`. */
import * as Context from 'effect/Context';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Random from 'effect/Random';

const WORDS = 4;
const WORD_RANGE = 2 ** 32;

/** 128 random bits as 32 hex digits. */
const randomId = Effect.gen(function* () {
  let id = '';
  for (let word = 0; word < WORDS; word += 1) {
    const bits = yield* Random.nextIntBetween(0, WORD_RANGE - 1, {halfOpen: true});
    id += bits.toString(16).padStart(8, '0');
  }
  return id;
});

export class Ids extends Context.Service<
  Ids,
  {
    /** A fresh id. Two calls never return the same one (up to chance). */
    readonly next: Effect.Effect<string>;
  }
>()('app/Ids') {
  static readonly layer = Layer.succeed(Ids, Ids.of({next: randomId}));
}
