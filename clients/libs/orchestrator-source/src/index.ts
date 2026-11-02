/**
 * Where the orchestrator screens read from: the `OrchestratorSource` interface and the ports the
 * machines take. The real implementation will be a gRPC source over the host gateway connection
 * (`@vibesys/connection`); until the gateway serves `OrchestratorService`, the screens read the
 * fixture source in `@vibesys/orchestrator-fixtures`.
 */
export {type HostPorts, portsOf, type Watch} from './ports.js';
export {type CancelSignal, type OrchestratorSource, SourceError} from './source.js';
