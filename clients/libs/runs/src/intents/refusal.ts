import {type ConfigurationFailed, HOST_UNSUPPORTED_MESSAGE} from '@vibesys/proto';

/** What a screen says of a HostCall the host refused: never the host's stderr. */
export function refusalMessage(failure: ConfigurationFailed): string {
  return failure.code === 'host_unsupported' ? HOST_UNSUPPORTED_MESSAGE : failure.message;
}
