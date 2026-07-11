import {describe, expect, it} from 'vitest';
import {parseInput} from './commands.js';

describe('parseInput', () => {
  it('keeps chat separate from steering', () => {
    expect(parseInput('what is happening?').request?.type).toBe('query.chat');
    expect(parseInput('/steer inspect the cache').request?.type).toBe('command.steer');
  });

  it('keeps inspection commands out of the public command surface', () => {
    expect(parseInput('/round 4').error).toBe('Unknown command: /round 4');
    expect(parseInput('/invocation abc').error).toBe('Unknown command: /invocation abc');
    expect(parseInput('/show workspace/file').error).toBe('Unknown command: /show workspace/file');
  });

  it('provides local help without a backend request', () => {
    expect(parseInput('/help')).toEqual({localView: 'help'});
  });
});
