/**
 * The press of Catch me up on a run. It is one of the user's commands (`commands.ts`): one press
 * sends one `RequestCatchUp` under one id, the app never sends it again by itself, and an outcome it
 * cannot tell (a lost link, no reply in time) stays `Unconfirmed` until the receipt for the id
 * arrives on the run stream, or until the user presses again, which sends the same command under
 * the same id. The host answers a command it already holds with its receipt, so the retry makes
 * no second check-in, and a press made while the viewer's last check-in is still being worked out
 * answers with that one.
 *
 * What this adds to a command is the run it was for and the answer in the words of this feature
 * (`CatchUpPress`): the check-in made, a refusal as data, or a failure. The host decides the
 * window, the facts and whether the lead is asked; the check-in then arrives on the run stream
 * and is read from the folded state (`rewind` in `state/orchestrator/check-ins.ts`).
 */
import {create} from '@bufbuild/protobuf';
import {RequestCatchUpSchema} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Atom from 'effect/reactivity/Atom';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {type CatchUpPress, pressOf} from '../state/index.js';
import type {Commands} from './commands.js';

export interface CatchUp {
  /**
   * Presses Catch me up on `runId`. A press still on its way is left alone; when the last press of the
   * run is `Unconfirmed` this sends that command again under its id instead of making a new one; any
   * other press is a new command. It never fails: how it went is `standing`.
   */
  readonly press: (runId: string) => Effect.Effect<void>;
  /** How the last press of a run stands; `undefined` for a run never pressed in this session. */
  readonly standing: (runId: string) => Atom.Atom<CatchUpPress | undefined>;
}

type Issued = Readonly<Record<string, string>>;

export const makeCatchUp = Effect.fn('CatchUp.make')(function* (commands: Commands) {
  /** The id of the last command pressed for each run. */
  const issued = yield* SubscriptionRef.make<Issued>({});
  const issuedAtom = Atom.keepAlive(Atom.subscriptionRef(issued));

  const standing = Atom.family((runId: string) =>
    Atom.make(get => {
      const id = get(issuedAtom)[runId];
      return id === undefined ? undefined : pressOf(get(commands.progress(id)));
    }),
  );

  const press = Effect.fn('CatchUp.press')(function* (runId: string) {
    const held = (yield* SubscriptionRef.get(issued))[runId];
    const last = held === undefined ? undefined : yield* commands.now(held);
    if (last?._tag === 'Sending') return;
    if (held !== undefined && last?._tag === 'Unconfirmed') {
      yield* commands.retry(held);
      return;
    }
    const id = yield* commands.submit({
      case: 'requestCatchUp',
      value: create(RequestCatchUpSchema, {runId}),
    });
    yield* SubscriptionRef.update(issued, all => ({...all, [runId]: id}));
  });

  const catchUp: CatchUp = {press, standing};
  return catchUp;
});
