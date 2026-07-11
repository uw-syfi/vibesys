import {describe, expect, it} from 'vitest';
import {parseInput} from './commands.js';

describe('parseInput', () => {
  it('only accepts the intentionally small command surface', () => {
    expect(parseInput('/history').request?.type).toBe('query.history');
    expect(parseInput('what is happening?').error).toContain('Unknown command');
    expect(parseInput('/steer inspect the cache').error).toContain('Unknown command');
  });

  it('keeps inspection commands out of the public command surface', () => {
    expect(parseInput('/round 4').error).toContain('Unknown command');
    expect(parseInput('/invocation abc').error).toContain('Unknown command');
    expect(parseInput('/show workspace/file').error).toContain('Unknown command');
  });

  it('provides local help without a backend request', () => {
    expect(parseInput('/help')).toEqual({localView: 'help'});
  });
});
