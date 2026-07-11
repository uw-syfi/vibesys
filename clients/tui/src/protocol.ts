import type {ProtocolDocument} from './protocol.generated.js';

export type ProtocolRequest = ProtocolDocument['request'];
export type ProtocolResponse = ProtocolDocument['response'];
export type RunEvent = ProtocolDocument['event'];
export type RunSnapshot = ProtocolDocument['snapshot'];
export type ServerMessage = ProtocolDocument['server_message'];

export type RequestInput = ProtocolRequest extends infer Request
  ? Request extends ProtocolRequest
    ? Omit<Request, 'protocol_version' | 'request_id' | 'timestamp'>
    : never
  : never;
