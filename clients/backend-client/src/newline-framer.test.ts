import {describe, expect, it} from 'bun:test';
import {NewlineFramer} from './newline-framer.js';

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
});
