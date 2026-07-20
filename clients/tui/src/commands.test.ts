import {describe, expect, it} from 'vitest';
import {parseInput, slashCommandRange, suggestSlashCommands} from './commands.js';

describe('parseInput', () => {
  it('accepts the intentionally small slash-command surface', () => {
    expect(parseInput('/history').request?.type).toBe('query.history');
    expect(parseInput('/perf')).toMatchObject({
      request: {type: 'query.performance'},
      responseView: 'perf',
    });
    expect(parseInput('/chat what changed in the latest round?')).toEqual({
      localView: 'chat',
      chatMessage: 'what changed in the latest round?',
    });
    expect(parseInput('/steer inspect the cache').error).toContain('Unknown command');
  });

  it('sends ordinary text to the supervision chat endpoint', () => {
    expect(parseInput('what is happening?')).toEqual({
      request: {type: 'query.chat', text: 'what is happening?'},
    });
    expect(parseInput('')).toEqual({error: 'Enter a question or use /help.'});
  });

  it('keeps inspection commands out of the public command surface', () => {
    expect(parseInput('/round 4').error).toContain('Unknown command');
    expect(parseInput('/invocation abc').error).toContain('Unknown command');
    expect(parseInput('/show workspace/file').error).toContain('Unknown command');
  });

  it('provides local help without a backend request', () => {
    expect(parseInput('/help')).toEqual({localView: 'help'});
  });

  it('opens chat without requiring an initial question', () => {
    expect(parseInput('/chat')).toEqual({localView: 'chat'});
    expect(parseInput('/chat   ')).toEqual({localView: 'chat'});
  });
});

describe('slash-command input helpers', () => {
  it('suggests available commands from a slash prefix', () => {
    expect(suggestSlashCommands('/').map(command => command.name)).toEqual([
      '/help',
      '/chat',
      '/history',
      '/perf',
    ]);
    expect(suggestSlashCommands('/hi').map(command => command.name)).toEqual(['/history']);
    expect(suggestSlashCommands('/history ')).toEqual([]);
    expect(suggestSlashCommands('history')).toEqual([]);
  });

  it('finds a leading slash-command token for syntax highlighting', () => {
    expect(slashCommandRange('/history')).toEqual({start: 0, end: 8});
    expect(slashCommandRange('/steer inspect the cache')).toEqual({start: 0, end: 6});
    expect(slashCommandRange('/')).toBeNull();
    expect(slashCommandRange('show /history')).toBeNull();
  });
});
