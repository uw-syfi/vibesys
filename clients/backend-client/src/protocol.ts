import type {ProtocolDocument} from './generated/protocol.generated.js';

export type ProtocolRequest = ProtocolDocument['request'];
export type ProtocolResponse = ProtocolDocument['response'];
export type RunEvent = ProtocolDocument['event'];
export type RunSnapshot = ProtocolDocument['snapshot'];
/** Run lifecycle statuses the backend reports. Source: `RunStatus` in `src/server/run_lifecycle.py`. */
export type RunStatus = RunSnapshot['status'];
export type ServerMessage = ProtocolDocument['server_message'];
export type Diagnostic = NonNullable<ProtocolResponse['diagnostic']>;
export type HypothesisEntry = NonNullable<ProtocolResponse['experiments']>[number];
export type ExperimentUpdate = NonNullable<ProtocolResponse['experiment_update']>;
export type ExperimentCursor = NonNullable<
  Extract<ProtocolRequest, {type?: 'query.experiments'}>['after']
>;
export type HypothesisRound = NonNullable<HypothesisEntry['rounds']>[number];
export type DesignRound = NonNullable<ProtocolResponse['design']>[number];
export type DesignFileChange = NonNullable<DesignRound['files']>[number];
export type ChatOptions = NonNullable<ProtocolResponse['chat_options']>;
export type ChatProviderOptions = NonNullable<ChatOptions['providers']>[number];
export type ChatModelOption = NonNullable<ChatProviderOptions['models']>[number];
export type TuiDefaults = NonNullable<ProtocolResponse['tui_defaults']>;

export type RequestInput = ProtocolRequest extends infer Request
  ? Request extends ProtocolRequest
    ? Omit<Request, 'protocol_version' | 'request_id' | 'timestamp'>
    : never
  : never;
