/**
 * What the host or gateway wrote about a failed call, read from the reply the main process relays.
 * It is for the main-process log: the app's screens get the typed failure (`OperationFailed`,
 * `IntentRefused`), whose message carries none of it.
 */
import {fromBinary} from '@bufbuild/protobuf';
import {
  HostCallResponseSchema,
  MutateResponseSchema,
  ReadResponseSchema,
  WatchResponseSchema,
} from '@vibesys/proto';
import {HOST_CALL_METHOD, MUTATE_METHOD, READ_METHOD, WATCH_METHOD} from './gateway.js';

/** The text of a failed reply to `method`, or null for a reply that did not fail or is no registry reply. */
export function failureDiagnostic(method: string, bytes: Uint8Array): string | null {
  try {
    switch (method) {
      case READ_METHOD:
        return failed(fromBinary(ReadResponseSchema, bytes).result);
      case MUTATE_METHOD:
        return failed(fromBinary(MutateResponseSchema, bytes).result);
      case WATCH_METHOD:
        return failed(fromBinary(WatchResponseSchema, bytes).result);
      case HOST_CALL_METHOD: {
        const {result} = fromBinary(HostCallResponseSchema, bytes);
        if (result.case !== 'configurationFailed') return null;
        const {code, message, stderr} = result.value;
        return `${code}: ${message}${stderr === '' ? '' : `\n${stderr}`}`;
      }
      default:
        return null;
    }
  } catch {
    return null;
  }
}

function failed(result: {
  readonly case: string | undefined;
  readonly value?: unknown;
}): string | null {
  if (result.case !== 'failure') return null;
  const {category, message} = result.value as {category: number; message: string};
  return `category ${category}: ${message}`;
}
