/** The typed client of the host's orchestrator service. */

import type {HostKey} from '@vibesys/hosts';
import {OrchestratorService} from '@vibesys/orchestrator-proto';
import type * as Effect from 'effect/Effect';
import {makeClient} from './client/make-client.js';
import type {GatewayTransport} from './transport/gateway-transport.js';

export const makeOrchestratorClient = (
  host: HostKey,
): Effect.Effect<OrchestratorClient, never, GatewayTransport> =>
  makeClient(OrchestratorService, host);

export type OrchestratorClient = Effect.Success<ReturnType<typeof makeClientOf>>;

// Names the client's type without a second declaration of the service's methods.
const makeClientOf = () => makeClient(OrchestratorService, '' as HostKey);
