/**
 * Asking a host why it does not serve. A host whose orchestrator could not open its state answers
 * every orchestrator call UNAVAILABLE, which reads as "try again" and is retried forever; but its
 * host-control service answers either way, and says what it refuses and why in a typed message.
 * The connection asks once after an UNAVAILABLE handshake, so that a refusal no retry changes is
 * shown with its cause and is not retried, and a host that is only starting or held by another
 * process still is.
 */
import {HostControlService, oneofIs} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import {type HostKey, refusalOf} from '../state/index.js';
import {makeClient} from './client/make-client.js';
import {Refused} from './errors.js';
import type {GatewayTransport} from './transport/gateway-transport.js';

/** The refusal the host says it has, or nothing when it says none or cannot be asked. */
export type WhyUnserved = Effect.Effect<Refused | undefined>;

export const makeWhyUnserved = (
  host: HostKey,
): Effect.Effect<WhyUnserved, never, GatewayTransport> =>
  Effect.gen(function* () {
    const client = yield* makeClient(HostControlService, host);
    return client.identify({clientRelease: ''}).pipe(
      Effect.map(({orchestrator}) => {
        if (!oneofIs(orchestrator, 'unserved', {serving: true, unserved: true})) return undefined;
        const refusal = refusalOf(orchestrator.value);
        return refusal === undefined ? undefined : new Refused(refusal);
      }),
      // A host that cannot be asked is left to the reconnect policy, as before.
      Effect.orElseSucceed(() => undefined),
    );
  });
