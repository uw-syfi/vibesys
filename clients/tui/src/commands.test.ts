import {describe, expect, it} from 'bun:test';
import {readFileSync} from 'node:fs';
import {
  availableCommands,
  COMMAND_NAMES,
  COMMAND_SPECS,
  type CommandSurface,
  chatHelpText,
  helpText,
  type ParsedCommand,
  parseCommand,
  slashCommandRange,
  suggestSlashCommands,
} from './commands.js';

const onCommand = (text: string): ParsedCommand => parseCommand(text, {surface: 'command'});
const onChat = (text: string): ParsedCommand => parseCommand(text, {surface: 'chat'});
const names = (commands: readonly {name: string}[]): string[] =>
  commands.map(command => command.name);

/** The commands both surfaces resolve identically, so parity is asserted once. */
const SHARED = [
  '/help',
  '/pause',
  '/resume',
  '/steer look at the cache',
  '/open-round',
  '/perf',
  '/design',
  '/todos',
  '/prompt',
  '/theme',
];

describe('parseCommand', () => {
  it('parses the command surface into discriminated actions', () => {
    expect(onCommand('/help')).toEqual({kind: 'help'});
    expect(onCommand('/pause')).toEqual({kind: 'request', request: {type: 'command.pause'}});
    expect(onCommand('/resume')).toEqual({kind: 'request', request: {type: 'command.resume'}});
    expect(onCommand('/steer prioritize the KV cache path')).toEqual({
      kind: 'request',
      request: {type: 'command.steer', text: 'prioritize the KV cache path'},
    });
    expect(onCommand('/open-round')).toEqual({kind: 'openRound'});
    expect(onCommand('/open-round --3')).toEqual({kind: 'openRound', round: 3});
    expect(onCommand('/open-round 3')).toEqual({kind: 'openRound', round: 3});
    expect(onCommand('/perf')).toMatchObject({
      kind: 'request',
      request: {type: 'query.performance'},
      responseView: 'perf',
      paneView: 'perf',
    });
    // Every visualization goes through the one pane mechanism, so /design
    // reaches the right pane exactly the way /perf does.
    expect(onCommand('/design')).toEqual({
      kind: 'request',
      request: {type: 'query.design'},
      paneView: 'design',
    });
    // Modal surfaces stay modal: no pane routing on any of them.
    expect(onCommand('/help')).not.toHaveProperty('paneView');
    expect(onCommand('/theme')).not.toHaveProperty('paneView');
    expect(onCommand('/chat')).not.toHaveProperty('paneView');
    expect(onCommand('/todos')).toEqual({kind: 'toggle', toggle: 'todos'});
    expect(onCommand('/prompt')).toEqual({kind: 'toggle', toggle: 'prompt'});
    expect(onCommand('/theme')).toEqual({kind: 'theme'});
    expect(onCommand('/theme solarized-light')).toEqual({
      kind: 'theme',
      themeName: 'solarized-light',
    });
    expect(onCommand('/chat')).toEqual({kind: 'openChat'});
    expect(onCommand('/chat what changed in the latest round?')).toEqual({
      kind: 'openChat',
      chatMessage: 'what changed in the latest round?',
    });
  });

  it('reports usage and validation errors from the registry', () => {
    expect(onCommand('/steer')).toEqual({kind: 'error', error: 'Usage: /steer <message>'});
    expect(onCommand('/steer   ')).toEqual({kind: 'error', error: 'Usage: /steer <message>'});
    expect(onCommand('/open-round latest')).toMatchObject({kind: 'error'});
    const badTheme = onCommand('/theme monokai');
    expect(badTheme.kind).toBe('error');
    if (badTheme.kind === 'error') {
      expect(badTheme.error).toContain('Unknown theme: monokai');
      expect(badTheme.error).toContain('catppuccin-mocha');
    }
  });

  it('diagnoses non-command and unknown input on the command surface', () => {
    expect(onCommand('')).toEqual({
      kind: 'error',
      error: 'Enter a slash command: try /help for the list.',
    });
    expect(onCommand('what is happening?')).toEqual({
      kind: 'error',
      error: 'Not a command: try /help, or ask in Experiment chat.',
    });
    expect(onCommand('/round 4')).toEqual({kind: 'unknown', text: '/round 4'});
    expect(onCommand('/invocation abc')).toEqual({kind: 'unknown', text: '/invocation abc'});
    expect(onCommand('/history')).toEqual({kind: 'unknown', text: '/history'});
  });
});

describe('argument-contract enforcement', () => {
  it('rejects trailing text on no-argument commands on the command bar', () => {
    // Before the registry refactor exact-match parsers rejected these; a
    // no-argument parser must not silently ignore a slash-prefixed phrase.
    expect(onCommand('/pause typo')).toEqual({kind: 'error', error: 'Usage: /pause'});
    expect(onCommand('/perf extra')).toEqual({kind: 'error', error: 'Usage: /perf'});
    expect(onCommand('/help ignored')).toEqual({kind: 'error', error: 'Usage: /help'});
    expect(onCommand('/resume now')).toEqual({kind: 'error', error: 'Usage: /resume'});
    expect(onCommand('/design later')).toEqual({kind: 'error', error: 'Usage: /design'});
  });

  it('rejects trailing text on no-argument commands in the chat', () => {
    expect(onChat('/pause typo')).toEqual({kind: 'error', error: 'Usage: /pause'});
    expect(onChat('/help ignored')).toEqual({kind: 'error', error: 'Usage: /help'});
    // A chat-only, state-changing command must not fire from a phrase either.
    expect(onChat('/clear definitely-not')).toEqual({kind: 'error', error: 'Usage: /clear'});
    expect(onChat('/model gpt')).toEqual({kind: 'error', error: 'Usage: /model'});
    expect(onChat('/switch elsewhere')).toEqual({kind: 'error', error: 'Usage: /switch'});
  });

  it('still accepts no-argument commands with no trailing text', () => {
    expect(onCommand('/pause')).toEqual({kind: 'request', request: {type: 'command.pause'}});
    // Trailing whitespace alone is not an argument.
    expect(onCommand('/pause   ')).toEqual({kind: 'request', request: {type: 'command.pause'}});
    expect(onChat('/clear')).toEqual({kind: 'chatClear'});
  });

  it('leaves commands that take arguments unaffected', () => {
    expect(onCommand('/steer look at the cache')).toEqual({
      kind: 'request',
      request: {type: 'command.steer', text: 'look at the cache'},
    });
    expect(onCommand('/theme solarized-light')).toEqual({
      kind: 'theme',
      themeName: 'solarized-light',
    });
    expect(onCommand('/open-round 3')).toEqual({kind: 'openRound', round: 3});
    // Optional-argument commands still resolve with no argument.
    expect(onCommand('/theme')).toEqual({kind: 'theme'});
    expect(onCommand('/open-round')).toEqual({kind: 'openRound'});
  });

  it('rejects an empty argument on required-argument commands with the registry usage', () => {
    expect(onCommand('/steer')).toEqual({kind: 'error', error: 'Usage: /steer <message>'});
    expect(onChat('/steer')).toEqual({kind: 'error', error: 'Usage: /steer <message>'});
  });

  /**
   * The cases above name the commands from the report. This one enumerates the
   * registry instead, so a command registered later cannot be the one that
   * still accepts a trailing phrase.
   */
  it('enforces the declared arity of every registered command on every surface', () => {
    for (const spec of COMMAND_SPECS) {
      const usage = `Usage: ${spec.usage ?? spec.name}`;
      for (const surface of spec.surfaces as readonly CommandSurface[]) {
        const parse = (text: string): ParsedCommand => parseCommand(text, {surface});
        const where = `${spec.name} on ${surface}`;
        if (spec.args === 'none') {
          expect({where, ...parse(`${spec.name} trailing-text`)}).toEqual({
            where,
            kind: 'error',
            error: usage,
          });
          // The bare command still resolves, so the check rejects arguments
          // rather than the command.
          expect({where, kind: parse(spec.name).kind}).not.toEqual({where, kind: 'error'});
        } else if (spec.args === 'required') {
          expect({where, ...parse(spec.name)}).toEqual({where, kind: 'error', error: usage});
        } else {
          expect({where, kind: parse(spec.name).kind}).not.toEqual({where, kind: 'error'});
        }
      }
    }
  });

  it('covers every arity the registry declares, so the loop above is not vacuous', () => {
    expect(new Set(COMMAND_SPECS.map(spec => spec.args))).toEqual(
      new Set(['none', 'optional', 'required']),
    );
  });
});

/**
 * The README is the only copy of the command list outside the registry, and it
 * is what a reader consults before the help text. Asserting it against the
 * registry is what keeps the two from drifting the way the pre-registry tables
 * did.
 */
describe('README command tables', () => {
  const readme = readFileSync(new URL('../README.md', import.meta.url), 'utf8');

  /** The `/name` of every table row in a section, deduplicated in document order. */
  const documentedCommands = (heading: string, nextHeading: string): string[] => {
    const start = readme.indexOf(heading);
    expect(start).toBeGreaterThanOrEqual(0);
    const end = readme.indexOf(nextHeading, start + heading.length);
    expect(end).toBeGreaterThan(start);
    const section = readme.slice(start, end);
    const documented: string[] = [];
    for (const line of section.split('\n')) {
      const name = /^\|\s*`(\/[a-z][a-z0-9-]*)/.exec(line)?.[1];
      if (name !== undefined && !documented.includes(name)) documented.push(name);
    }
    return documented;
  };

  const specNames = (predicate: (surfaces: readonly CommandSurface[]) => boolean): string[] =>
    COMMAND_SPECS.filter(spec => predicate(spec.surfaces)).map(spec => spec.name);

  it('documents exactly the commands the command bar offers, in registry order', () => {
    expect(documentedCommands('| Command | Behavior |', 'The chat composer adds')).toEqual(
      specNames(surfaces => surfaces.includes('command')),
    );
  });

  it('documents exactly the chat-only commands, in registry order', () => {
    expect(documentedCommands('The chat composer adds', '### Experiment log')).toEqual(
      specNames(surfaces => !surfaces.includes('command')),
    );
  });
});

describe('cross-surface parity', () => {
  it('resolves shared commands identically on the command bar and in the chat', () => {
    for (const text of SHARED) {
      expect(onChat(text)).toEqual(onCommand(text));
    }
  });

  it('matches command names case-insensitively on both surfaces', () => {
    const resume = {kind: 'request', request: {type: 'command.resume'}} as const;
    expect(onCommand('/Pause')).toEqual(onCommand('/pause'));
    expect(onChat('/PAUSE')).toEqual(onCommand('/pause'));
    expect(onChat('/Resume')).toEqual(resume);
  });

  it('produces the same /steer usage error wherever it is typed', () => {
    expect(onChat('/steer')).toEqual(onCommand('/steer'));
  });
});

describe('chat-only commands', () => {
  it('resolves the chat thread commands only in the chat', () => {
    expect(onChat('/clear')).toEqual({kind: 'chatClear'});
    expect(onChat('/model')).toEqual({kind: 'chatModel'});
    expect(onChat('/switch')).toEqual({kind: 'chatSwitch'});
    // On the command bar those names are not registered, so they are unknown.
    expect(onCommand('/clear')).toEqual({kind: 'unknown', text: '/clear'});
    expect(onCommand('/model')).toEqual({kind: 'unknown', text: '/model'});
    expect(onCommand('/switch')).toEqual({kind: 'unknown', text: '/switch'});
  });

  it('resumes the paused run from /resume on both surfaces, and switches threads with /switch', () => {
    // /resume no longer collides: it means the run everywhere, and the chat's
    // own thread switch has its own name.
    expect(onChat('/resume')).toEqual({kind: 'request', request: {type: 'command.resume'}});
    expect(onChat('/switch')).toEqual({kind: 'chatSwitch'});
  });

  it('answers unknown slash input as unknown, leaving each surface to render its own help', () => {
    expect(onChat('/threads')).toEqual({kind: 'unknown', text: '/threads'});
    expect(onCommand('/threads')).toEqual({kind: 'unknown', text: '/threads'});
  });
});

describe('suggestions filtered by surface', () => {
  it('suggests the command-bar commands in registry order', () => {
    expect(names(suggestSlashCommands('/', {surface: 'command'}))).toEqual([
      '/help',
      '/chat',
      '/pause',
      '/resume',
      '/steer',
      '/open-round',
      '/perf',
      '/design',
      '/todos',
      '/prompt',
      '/theme',
    ]);
    expect(names(suggestSlashCommands('/h', {surface: 'command'}))).toEqual(['/help']);
    expect(names(suggestSlashCommands('/open', {surface: 'command'}))).toEqual(['/open-round']);
    expect(suggestSlashCommands('/e', {surface: 'command'})).toEqual([]);
    expect(suggestSlashCommands('/perf ', {surface: 'command'})).toEqual([]);
    expect(suggestSlashCommands('perf', {surface: 'command'})).toEqual([]);
  });

  it('leads the chat with its thread commands, then the forwarded globals', () => {
    const chat = names(suggestSlashCommands('/', {surface: 'chat'}));
    expect(chat.slice(0, 3)).toEqual(['/clear', '/model', '/switch']);
    // The headline fix: the chat now suggests the global commands it forwards.
    expect(chat).toContain('/pause');
    expect(chat).toContain('/perf');
    // /chat is command-bar only: there is nothing for it to open inside the chat.
    expect(chat).not.toContain('/chat');
    expect(names(suggestSlashCommands('/pa', {surface: 'chat'}))).toEqual(['/pause']);
    expect(names(suggestSlashCommands('/m', {surface: 'chat'}))).toEqual(['/model']);
  });

  it('drops /chat where the chat is already docked', () => {
    const docked = names(availableCommands({surface: 'command', chatDocked: true}));
    expect(docked).not.toContain('/chat');
    expect(docked).toContain('/perf');
    expect(suggestSlashCommands('/c', {surface: 'command', chatDocked: true})).toEqual([]);
    // Hidden from the list, still parsed: the chat is the point.
    expect(parseCommand('/chat', {surface: 'command', chatDocked: true})).toEqual({
      kind: 'openChat',
    });
  });
});

describe('registry-derived help and helpers', () => {
  it('generates command-bar help without the stale planned block', () => {
    const help = helpText();
    expect(help).toContain('/help');
    expect(help).toContain('/design');
    expect(help).not.toContain('Planned');
    expect(help).not.toContain('/invocation');
    expect(help).not.toContain('/round');
  });

  it('generates chat help from the chat surface', () => {
    const help = chatHelpText();
    expect(help).toContain('/clear');
    expect(help).toContain('/model');
    expect(help).toContain('/switch');
    expect(help).toContain('/pause');
  });

  it('exposes the canonical name per command', () => {
    expect(COMMAND_NAMES.todos).toBe('/todos');
    expect(COMMAND_NAMES.prompt).toBe('/prompt');
    expect(COMMAND_NAMES['open-round']).toBe('/open-round');
  });

  it('finds a leading slash-command token for syntax highlighting', () => {
    expect(slashCommandRange('/open-round')).toEqual({start: 0, end: 11});
    expect(slashCommandRange('/steer inspect the cache')).toEqual({start: 0, end: 6});
    expect(slashCommandRange('/')).toBeNull();
    expect(slashCommandRange('show /perf')).toBeNull();
  });
});
