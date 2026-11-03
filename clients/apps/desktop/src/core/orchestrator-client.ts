/** The typed client of the host's orchestrator service. */

import {OrchestratorService} from '@vibesys/orchestrator-proto';
import type * as Effect from 'effect/Effect';
import type {HostKey} from '../state/index.js';
import {type EffectClient, makeClient} from './client/make-client.js';
import type {GatewayTransport} from './transport/gateway-transport.js';

export const makeOrchestratorClient = (
  host: HostKey,
): Effect.Effect<OrchestratorClient, never, GatewayTransport> =>
  makeClient(OrchestratorService, host);

export type OrchestratorClient = EffectClient<typeof OrchestratorService.method>;
