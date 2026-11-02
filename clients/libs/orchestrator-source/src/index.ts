/**
 * Where the orchestrator screens read from: the `OrchestratorSource` interface, the ports the
 * machines take, and the real source, `GatewaySource`, over the host gateway connection that
 * `vibesysd` serves `OrchestratorService` on. Tests and screenshots read the fixture source in
 * `@vibesys/orchestrator-fixtures` through the same interface.
 */
export {
  type GatewayCaller,
  GatewaySource,
  MAX_QUEUED_FRAMES,
  PROTOCOL_VERSION,
  sourceErrorOf,
} from './gateway-source.js';
export {type HostPorts, portsOf, type Watch} from './ports.js';
export {type CancelSignal, type OrchestratorSource, SourceError} from './source.js';
