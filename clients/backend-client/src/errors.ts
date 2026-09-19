import type {Diagnostic, ProtocolErrorMessage} from './protocol.js';

/** A failed server response, including its optional structured diagnostic. */
export class ServerError extends Error {
  constructor(
    message: string,
    readonly diagnostic: Diagnostic | null = null,
    /** The wire error code when the server reported a protocol error; otherwise null. */
    readonly code: string | null = null,
  ) {
    super(message);
    this.name = 'ServerError';
  }
}

/** Surface a server `protocolError` as a `ServerError`, naming a version mismatch clearly. */
export function protocolErrorToServerError(error: ProtocolErrorMessage): ServerError {
  const message =
    error.code === 'protocol_version_unsupported'
      ? `Server rejected the client protocol version: ${error.message}`
      : error.message;
  return new ServerError(message, error.diagnostic ?? null, error.code);
}
