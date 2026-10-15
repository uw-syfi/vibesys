import {describe, expect, test} from 'bun:test';
import {parseRunLogsRequest} from './run-logs.js';

const READ = {
  instance_id: '0123456789ab',
  stream_id: '000001',
  start: {offset: 12},
  max_bytes: 65536,
  wait_ms: 25000,
};

describe('parseRunLogsRequest', () => {
  test('accepts a listing and a read from an offset or a tail', () => {
    expect(parseRunLogsRequest({kind: 'list'})).toEqual({kind: 'list'});
    expect(parseRunLogsRequest({kind: 'read', read: READ})).toEqual({kind: 'read', read: READ});
    const tail = {...READ, start: {tail_bytes: 4096}};
    expect(parseRunLogsRequest({kind: 'read', read: tail})).toEqual({kind: 'read', read: tail});
  });

  test('rejects an unknown key at every level, and every wrong type', () => {
    const refused: unknown[] = [
      null,
      'list',
      {kind: 'list', host: 'gpu'},
      {kind: 'tail'},
      {kind: 'read'},
      {kind: 'read', read: READ, extra: 1},
      {kind: 'read', read: {...READ, command: 'rm'}},
      {kind: 'read', read: {...READ, start: {offset: 1, tail_bytes: 2}}},
      {kind: 'read', read: {...READ, start: {offset: -1}}},
      {kind: 'read', read: {...READ, start: {offset: 1.5}}},
      {kind: 'read', read: {...READ, max_bytes: '64'}},
      {kind: 'read', read: {...READ, wait_ms: Number.NaN}},
      {kind: 'read', read: {...READ, instance_id: 7}},
      {kind: 'read', read: [READ]},
    ];
    for (const value of refused) expect(parseRunLogsRequest(value)).toBeNull();
  });
});
