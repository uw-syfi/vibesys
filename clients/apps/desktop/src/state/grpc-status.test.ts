import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {callStatus} from './grpc-status.js';

const UNAVAILABLE = 14;

/** What a peer's trailers may carry as `grpc-status`: a status, or garbage, or nothing. */
const anyStatus = fc.option(fc.oneof(fc.string(), fc.integer({min: -5, max: 2000}).map(String)), {
  nil: undefined,
});

describe('the status a call ends with', () => {
  it('is OK only for status 0', () => {
    expect(callStatus('0', '')).toBeUndefined();
    expect(callStatus('0', 'fine')).toBeUndefined();
  });

  it('carries the peer status and its words unchanged: a refusal is the host answering', () => {
    fc.assert(
      fc.property(fc.integer({min: 1, max: 999}), fc.string({minLength: 1}), (code, detail) => {
        expect(callStatus(String(code), detail)).toEqual({code, message: detail});
      }),
    );
  });

  it('treats a stream that ended with no status, or none that is a number, as a lost link', () => {
    fc.assert(
      fc.property(anyStatus, fc.string(), (status, detail) => {
        const end = callStatus(status, detail);
        if (status !== undefined && /^\d{1,3}$/.test(status)) return;
        expect(end?.code).toBe(UNAVAILABLE);
      }),
    );
  });

  it('is never a status the host did not send: only the peer, or the link, speaks', () => {
    fc.assert(
      fc.property(anyStatus, fc.string(), (status, detail) => {
        const end = callStatus(status, detail);
        if (end === undefined) expect(Number(status)).toBe(0);
        else expect([UNAVAILABLE, Number(status)]).toContain(end.code);
      }),
    );
  });
});
