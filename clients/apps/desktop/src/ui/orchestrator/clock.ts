/** The screens' wall clock: milliseconds since the epoch, once a second, from Effect's `Clock`. */

import type * as Cause from 'effect/Cause';
import * as Clock from 'effect/Clock';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as Stream from 'effect/Stream';

const TICK = '1 second';

export const nowMs: Atom.Atom<AsyncResult.AsyncResult<number, Cause.NoSuchElementError>> =
  Atom.make(
    Stream.fromEffect(Clock.currentTimeMillis).pipe(
      Stream.concat(Stream.tick(TICK).pipe(Stream.mapEffect(() => Clock.currentTimeMillis))),
    ),
  );
