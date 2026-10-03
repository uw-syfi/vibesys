import {describe, expect, it} from 'bun:test';
import {BackendClientError} from './errors.js';
import {NewlineFramer} from './newline-framer.js';

const CAP = 4 * 1024 * 1024;

describe('NewlineFramer', () => {
  it('emits complete frames and retains an incomplete suffix', () => {
    const framer = new NewlineFramer();

    expect(framer.push('one\ntwo\npar')).toEqual(['one', 'two']);
    expect(framer.push('tial\n')).toEqual(['partial']);
  });

  it('preserves empty frames for the protocol consumer to interpret', () => {
    const framer = new NewlineFramer();

    expect(framer.push('\n\n')).toEqual(['', '']);
  });

  it('reassembles the same frames at every chunk boundary', () => {
    const input = '{"first":1}\n{"second":2}\n';
    for (let boundary = 0; boundary <= input.length; boundary += 1) {
      const framer = new NewlineFramer();
      expect([
        ...framer.push(input.slice(0, boundary)),
        ...framer.push(input.slice(boundary)),
      ]).toEqual(['{"first":1}', '{"second":2}']);
    }
  });

  it('holds a large newline-less remainder up to the cap', () => {
    const framer = new NewlineFramer();
    // A frame right at the cap is still a valid (if improbable) message.
    expect(framer.push('x'.repeat(CAP))).toEqual([]);
    expect(framer.push('\n')).toEqual(['x'.repeat(CAP)]);
  });

  it('rejects a newline-less remainder past the cap with a typed parse error', () => {
    const framer = new NewlineFramer();
    let thrown: unknown;
    try {
      framer.push('x'.repeat(CAP + 1));
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toBeInstanceOf(BackendClientError);
    expect((thrown as BackendClientError).kind).toBe('parse');
  });
});
