import {expect, test} from 'bun:test';
import {parseGatewayRequest} from './index.js';

test('generic gateway calls validate all routing keys and preserve protobuf bytes', () => {
  const request = {
    kind: 'stream' as const,
    host: 'ssh:host-a',
    method: '/vibesys.api.v1.HostService/Tail',
    bytes: new Uint8Array([1, 2]),
  };
  expect(parseGatewayRequest(request)).toEqual(request);
  for (const change of [
    {kind: 'bidi'},
    {host: '-oProxyCommand=x'},
    {method: 'Tail'},
    {bytes: []},
    {extra: true},
  ]) {
    expect(() => parseGatewayRequest({...request, ...change})).toThrow();
  }
});
