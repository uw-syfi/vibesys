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
    const read = {kind: 'read', token: 'r1', read: READ} as const;
    expect(parseRunLogsRequest(read)).toEqual(read);
    const tail = {kind: 'read', token: 'r2', read: {...READ, start: {tail_bytes: 4096}}} as const;
    expect(parseRunLogsRequest(tail)).toEqual(tail);
  });

  test('accepts a withdrawal and a read named by any token in the alphabet up to its bound', () => {
    const alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-';
    for (let length = 1; length <= 64; length += 1) {
      const token = Array.from({length}, (_, index) => alphabet[(index * 7 + length) % 64]).join(
        '',
      );
      expect(parseRunLogsRequest({kind: 'withdraw', token})).toEqual({kind: 'withdraw', token});
      expect(parseRunLogsRequest({kind: 'read', token, read: READ})).toEqual({
        kind: 'read',
        token,
        read: READ,
      });
    }
  });

  test('rejects a missing, empty, overlong, non-string, or out-of-alphabet token', () => {
    const tokens: unknown[] = [undefined, '', 'x'.repeat(65), 7, null, 'a b', 'a/b', 'é', 'a\n'];
    for (const token of tokens) {
      const named = token === undefined ? {} : {token};
      expect(parseRunLogsRequest({kind: 'withdraw', ...named})).toBeNull();
      expect(parseRunLogsRequest({kind: 'read', ...named, read: READ})).toBeNull();
    }
    expect(parseRunLogsRequest({kind: 'withdraw', token: 'r1', read: READ})).toBeNull();
    expect(parseRunLogsRequest({kind: 'withdraw', token: 'r1', extra: 1})).toBeNull();
  });

  test('rejects an unknown key at every level, and every wrong type', () => {
    const refused: unknown[] = [
      null,
      'list',
      {kind: 'list', host: 'gpu'},
      {kind: 'tail'},
      {kind: 'read'},
      {kind: 'read', read: READ},
      {kind: 'read', token: 'r1', read: READ, extra: 1},
      {kind: 'read', token: 'r1', read: {...READ, command: 'rm'}},
      {kind: 'read', token: 'r1', read: {...READ, start: {offset: 1, tail_bytes: 2}}},
      {kind: 'read', token: 'r1', read: {...READ, start: {offset: -1}}},
      {kind: 'read', token: 'r1', read: {...READ, start: {offset: 1.5}}},
      {kind: 'read', token: 'r1', read: {...READ, max_bytes: '64'}},
      {kind: 'read', token: 'r1', read: {...READ, wait_ms: Number.NaN}},
      {kind: 'read', token: 'r1', read: {...READ, instance_id: 7}},
      {kind: 'read', token: 'r1', read: [READ]},
    ];
    for (const value of refused) expect(parseRunLogsRequest(value)).toBeNull();
  });
});
