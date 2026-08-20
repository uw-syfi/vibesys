import {describe, expect, it} from 'bun:test';
import {
  availableCommands,
  helpText,
  parseInput,
  slashCommandRange,
  suggestSlashCommands,
} from './commands.js';

describe('parseInput', () => {
  it('accepts the intentionally small slash-command surface', () => {
    expect(parseInput('/open-round')).toEqual({openRound: {}});
    expect(parseInput('/open-round --3')).toEqual({openRound: {round: 3}});
    expect(parseInput('/open-round 3')).toEqual({openRound: {round: 3}});
    expect(parseInput('/open-round latest').error).toContain('Unknown round: latest');
    // Visualization commands opt into the right pane through one field.
    expect(parseInput('/perf')).toMatchObject({
      request: {type: 'query.performance'},
      paneView: 'perf',
    });
    // Modal surfaces stay modal: no pane routing on any of them.
    expect(parseInput('/help').paneView).toBeUndefined();
    expect(parseInput('/theme').paneView).toBeUndefined();
    expect(parseInput('/chat').paneView).toBeUndefined();
    expect(parseInput('/nope').paneView).toBeUndefined();
    expect(parseInput('/perf')).toMatchObject({
      request: {type: 'query.performance'},
      responseView: 'perf',
    });
    expect(parseInput('/chat what changed in the latest round?')).toEqual({
      localView: 'chat',
      chatMessage: 'what changed in the latest round?',
    });
  });

  it('parses run-control commands', () => {
    expect(parseInput('/pause')).toEqual({request: {type: 'command.pause'}});
    expect(parseInput('/resume')).toEqual({request: {type: 'command.resume'}});
    expect(parseInput('/steer prioritize the KV cache path')).toEqual({
      request: {type: 'command.steer', text: 'prioritize the KV cache path'},
    });
  });

  it('requires a message for /steer', () => {
    expect(parseInput('/steer').error).toContain('Usage: /steer');
    expect(parseInput('/steer   ').error).toContain('Usage: /steer');
  });

  it('sends ordinary text to the supervision chat endpoint', () => {
    expect(parseInput('what is happening?')).toEqual({
      request: {type: 'query.chat', text: 'what is happening?'},
    });
    expect(parseInput('')).toEqual({error: 'Enter a question or use /help.'});
  });

  it('rejects the removed experiment-log commands', () => {
    expect(parseInput('/history').error).toContain('Unknown command: /history');
    expect(parseInput('/history rounds').error).toContain('Unknown command: /history rounds');
    expect(parseInput('/experiments').error).toContain('Unknown command: /experiments');
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

  it('lists themes bare and selects a known theme by name', () => {
    expect(parseInput('/theme')).toEqual({localView: 'theme'});
    expect(parseInput('/theme   ')).toEqual({localView: 'theme'});
    expect(parseInput('/theme solarized-light')).toEqual({
      localView: 'theme',
      themeName: 'solarized-light',
    });
  });

  it('rejects an unknown theme name with the available list', () => {
    const parsed = parseInput('/theme monokai');
    expect(parsed.error).toContain('Unknown theme: monokai');
    expect(parsed.error).toContain('catppuccin-mocha');
    expect(parsed.localView).toBeUndefined();
  });
});

describe('command surface by view', () => {
  it('drops /chat where the chat is already a pane of the view', () => {
    const docked = availableCommands({chatDocked: true}).map(command => command.name);

    expect(docked).not.toContain('/chat');
    expect(docked).toContain('/perf');
    expect(helpText({chatDocked: true})).not.toContain('/chat');
    expect(suggestSlashCommands('/c', {chatDocked: true})).toEqual([]);
  });

  it('offers /chat everywhere the chat is not already on screen', () => {
    expect(availableCommands().map(command => command.name)).toContain('/chat');
    expect(helpText()).toContain('/chat');
    expect(suggestSlashCommands('/c').map(command => command.name)).toEqual(['/chat']);
  });

  it('still accepts /chat when it is not offered, since the chat is the point', () => {
    // Hidden from the list, not removed from the client.
    expect(parseInput('/chat')).toEqual({localView: 'chat'});
  });
});

describe('slash-command input helpers', () => {
  it('suggests available commands from a slash prefix', () => {
    expect(suggestSlashCommands('/').map(command => command.name)).toEqual([
      '/help',
      '/chat',
      '/pause',
      '/resume',
      '/steer',
      '/open-round',
      '/perf',
      '/theme',
    ]);
    expect(suggestSlashCommands('/h').map(command => command.name)).toEqual(['/help']);
    expect(suggestSlashCommands('/e')).toEqual([]);
    expect(suggestSlashCommands('/open').map(command => command.name)).toEqual(['/open-round']);
    expect(suggestSlashCommands('/perf ')).toEqual([]);
    expect(suggestSlashCommands('perf')).toEqual([]);
  });

  it('finds a leading slash-command token for syntax highlighting', () => {
    expect(slashCommandRange('/open-round')).toEqual({start: 0, end: 11});
    expect(slashCommandRange('/steer inspect the cache')).toEqual({start: 0, end: 6});
    expect(slashCommandRange('/')).toBeNull();
    expect(slashCommandRange('show /perf')).toBeNull();
  });
});
