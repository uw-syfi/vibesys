import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge, GatewayCall} from '../gateway.js';

/** A byte-preserving bridge: tests choose when the peer answers each owned call. */
export class FakeGatewayBridge implements GatewayBridge {
  readonly calls: Array<{
    method: string;
    bytes: Uint8Array;
    resolve: (bytes: Uint8Array) => void;
    reject: (error: Error) => void;
    cancelled: boolean;
  }> = [];
  readonly streams: Array<{
    method: string;
    bytes: Uint8Array;
    receive: (bytes: Uint8Array) => void;
    ended: (error?: string, status?: number) => void;
    cancelled: boolean;
  }> = [];
  unary(_host: HostKey, method: string, bytes: Uint8Array): GatewayCall<Uint8Array> {
    const {promise, resolve, reject} = Promise.withResolvers<Uint8Array>();
    const call = {method, bytes, resolve, reject, cancelled: false};
    this.calls.push(call);
    return {
      result: promise,
      cancel: () => {
        call.cancelled = true;
      },
    };
  }
  stream(
    _host: HostKey,
    method: string,
    bytes: Uint8Array,
    receive: (bytes: Uint8Array) => void,
    ended: (error?: string, status?: number) => void,
  ) {
    const stream = {method, bytes, receive, ended, cancelled: false};
    this.streams.push(stream);
    return {
      cancel: () => {
        stream.cancelled = true;
      },
    };
  }
}

export {FakeGatewayPort} from './gateway-port.js';
