/**
 * Asking a host what stands in the way of a run. A host whose orchestrator could not open its state
 * answers every orchestrator call UNAVAILABLE, which reads as "try again" and is retried forever; but
 * its host-control service answers either way, and says what it refuses and why in a typed message.
 * The same answer says whether every part a run needs is there and new enough (the program that
 * holds an agent session, the skills, the job tool), so a host that serves but could not run a run is
 * caught by the same question, before any agent spends tokens.
 *
 * The connection asks once after an UNAVAILABLE handshake, so that a refusal no retry changes is
 * shown with its cause and is not retried, and a host that is only starting or held by another
 * process still is. It asks once more when the link is Ready, and a start asks again before it sends.
 */
import {HostControlService, oneofIs} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import {type HostKey, readinessOf, refusalOf} from '../state/index.js';
import {makeClient} from './client/make-client.js';
import {Refused} from './errors.js';
import type {GatewayTransport} from './transport/gateway-transport.js';

/** The refusal the host says it has, or nothing when it says none or cannot be asked. */
export type WhyUnserved = Effect.Effect<Refused | undefined>;

/** What a host says stands in the way of a run: it does not serve, or a part is missing or too old. */
export type HostBlock = Effect.Effect<Refused | undefined>;

export interface HostReadiness {
  /** Why the host does not serve the orchestrator, for a handshake that came back UNAVAILABLE. */
  readonly whyUnserved: WhyUnserved;
  /** Everything that stops a run from starting on the host, asked now. */
  readonly blocked: HostBlock;
}

/** A host that cannot be asked is left to the reconnect policy, as before. */
export const makeHostReadiness = (
  host: HostKey,
): Effect.Effect<HostReadiness, never, GatewayTransport> =>
  Effect.gen(function* () {
    const client = yield* makeClient(HostControlService, host);
    const identify = client.identify({clientRelease: ''});
    return {
      whyUnserved: identify.pipe(
        Effect.map(({orchestrator}) => {
          if (!oneofIs(orchestrator, 'unserved', {serving: true, unserved: true})) return undefined;
          const refusal = refusalOf(orchestrator.value);
          return refusal === undefined ? undefined : new Refused(refusal);
        }),
        Effect.orElseSucceed(() => undefined),
      ),
      blocked: identify.pipe(
        Effect.map(id => {
          const refusal = readinessOf(id);
          return refusal === undefined ? undefined : new Refused(refusal);
        }),
        Effect.orElseSucceed(() => undefined),
      ),
    };
  });
