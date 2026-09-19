import {describe, expect, it} from 'bun:test';
import {create, fromJson, type JsonValue} from '@bufbuild/protobuf';
import {ValueSchema} from '@bufbuild/protobuf/wkt';
import {
  type CommandResultPayload,
  type ToolResultData,
  ToolResultDataSchema,
} from '@vibesys/backend-client';
import {
  elapsedLabel,
  jsonShapeSummary,
  promptPreview,
  toolCallPreview,
  toolOutputPreview,
  toolResultPreview,
  unwrapShellCommand,
} from './previews.js';

function jsonPayload(value: JsonValue): ToolResultData['payload'] {
  return create(ToolResultDataSchema, {
    payload: {case: 'json', value: {value: fromJson(ValueSchema, value)}},
  }).payload;
}

function commandPayload(
  init: Partial<Omit<CommandResultPayload, '$typeName'>>,
): ToolResultData['payload'] {
  return create(ToolResultDataSchema, {
    payload: {case: 'command', value: {stdout: '', stderr: '', ...init}},
  }).payload;
}

describe('conversation previews', () => {
  it('formats and truncates typed tool arguments without changing the source data', () => {
    const args = {
      text: 'x'.repeat(200),
      nested: {count: 3, flags: [true, false]},
    };
    const preview = toolCallPreview('Edit', args);

    expect(preview).toContain(`text="${'x'.repeat(80)}..."`);
    expect(preview).toContain('nested={"count":3,"flags":[true,false]}');
    expect(args.text).toHaveLength(200);
    expect(args.nested).toEqual({count: 3, flags: [true, false]});
  });

  it('limits tool output without discarding the underlying content', () => {
    const content = Array.from({length: 20}, (_, index) => `line ${index + 1}`).join('\n');
    const preview = toolOutputPreview(content);

    expect(preview.content).toContain('line 6');
    expect(preview.content).not.toContain('line 7');
    expect(preview).toMatchObject({hiddenLines: 14, collapsible: true});
    expect(content).toContain('line 20');
  });

  it('pretty-prints JSON responses and restores the complete value when expanded', () => {
    const content = JSON.stringify(
      Object.fromEntries(Array.from({length: 10}, (_, index) => [`field_${index}`, index])),
    );

    const collapsed = toolOutputPreview(content);
    const expanded = toolOutputPreview(content, true);

    expect(collapsed.content).toStartWith('{\n  "field_0": 0,');
    expect(collapsed.content).not.toContain('"field_9"');
    expect(collapsed.hiddenLines).toBeGreaterThan(0);
    expect(expanded.content).toContain('  "field_9": 9\n}');
    expect(expanded).toMatchObject({hiddenLines: 0, hiddenCharacters: 0, collapsible: true});
  });

  it('collapses long single-line responses by character count', () => {
    const content = 'x'.repeat(1_000);
    const preview = toolOutputPreview(content);

    expect(preview.content).toHaveLength(600);
    expect(preview).toMatchObject({hiddenLines: 0, hiddenCharacters: 400, collapsible: true});
  });

  it('collapses and expands long prompts', () => {
    const content = Array.from({length: 20}, (_, index) => `prompt line ${index + 1}`).join('\n');

    expect(promptPreview(content, false)).toMatchObject({hiddenLines: 8});
    expect(promptPreview(content, false).content).not.toContain('prompt line 13');
    expect(promptPreview(content, true).content).toContain('prompt line 20');
  });
});

describe('typed tool result previews', () => {
  it('pretty-prints a json payload from the parsed value without re-sniffing', () => {
    // Python-repr content would defeat the string sniffer; the payload wins.
    const preview = toolResultPreview("{'rows': [1, 2]}", jsonPayload({rows: [1, 2]}));

    expect(preview.content).toBe('{\n  "rows": [\n    1,\n    2\n  ]\n}');
  });

  it('lays out a command payload as stdout, labeled stderr, and exit code', () => {
    const preview = toolResultPreview(
      'build output\n',
      commandPayload({
        stdout: 'build output\n',
        stderr: 'warning: deprecated\n',
        exitCode: 2,
        duration: 1.5,
      }),
    );

    // A blank line before the label so stderr reads as its own section.
    expect(preview.content).toBe('build output\n\nstderr:\nwarning: deprecated\nexit code: 2');
  });

  it('omits the stderr label and exit-code line when they carry nothing', () => {
    const preview = toolResultPreview('ok', commandPayload({stdout: 'ok', stderr: ''}));

    expect(preview.content).toBe('ok');
  });

  it('falls back to the raw content when a command payload is empty', () => {
    const preview = toolResultPreview('raw text', commandPayload({stdout: '', stderr: ''}));

    expect(preview.content).toBe('raw text');
  });

  it('keeps the string-sniffing fallback for events without a payload', () => {
    const json = JSON.stringify({field: 'value'});

    expect(toolResultPreview(json, undefined).content).toBe('{\n  "field": "value"\n}');
    expect(toolResultPreview('plain text', null).content).toBe('plain text');
  });

  it('collapses long payload-rendered output like the fallback path', () => {
    const value = Object.fromEntries(Array.from({length: 20}, (_, index) => [`k${index}`, index]));

    const collapsed = toolResultPreview('irrelevant', jsonPayload(value));
    const expanded = toolResultPreview('irrelevant', jsonPayload(value), true);

    expect(collapsed.collapsible).toBe(true);
    expect(collapsed.hiddenLines).toBeGreaterThan(0);
    expect(expanded.content).toContain('"k19": 19');
  });
});

describe('shell tool call previews', () => {
  it('strips the /bin/bash -lc wrapper the codex CLI writes around a command', () => {
    expect(unwrapShellCommand('/bin/bash -lc "cargo test --workspace"')).toBe(
      'cargo test --workspace',
    );
    expect(unwrapShellCommand("bash -lc 'cargo build --release'")).toBe('cargo build --release');
    expect(unwrapShellCommand('sh -c "ls -la"')).toBe('ls -la');
    expect(unwrapShellCommand('/usr/bin/zsh -lc "pytest -q"')).toBe('pytest -q');
  });

  it('keeps quotes that belong to the command instead of the wrapper', () => {
    expect(unwrapShellCommand(`bash -lc 'grep -rn "fn main" src'`)).toBe('grep -rn "fn main" src');
    // Only one pair comes off, and only when it matches at both ends.
    expect(unwrapShellCommand('bash -lc "echo \'hi\'"')).toBe("echo 'hi'");
    expect(unwrapShellCommand('bash -lc "unterminated')).toBe('"unterminated');
  });

  it('leaves a command that was never wrapped alone', () => {
    expect(unwrapShellCommand('cargo bench -- --save-baseline main')).toBe(
      'cargo bench -- --save-baseline main',
    );
    // `bashful` is not a shell, and the flag has to be -c or -lc.
    expect(unwrapShellCommand('bashful -lc "x"')).toBe('bashful -lc "x"');
    expect(unwrapShellCommand('bash --login -c "x"')).toBe('bash --login -c "x"');
  });

  it('renders an execute call as the bare command, not a key=value pair', () => {
    expect(toolCallPreview('execute', {command: '/bin/bash -lc "cargo test -q"'})).toBe(
      '→ execute cargo test -q\n',
    );
    expect(toolCallPreview('shell', {command: 'ls'})).toBe('→ shell ls\n');
  });

  it("appends claude's Bash description as a shell comment", () => {
    expect(toolCallPreview('Bash', {command: 'pytest -q', description: 'Run the tests'})).toBe(
      '→ Bash pytest -q  # Run the tests\n',
    );
  });

  it('falls back to the generic rendering when a shell call has no command', () => {
    expect(toolCallPreview('execute', {argv: ['ls']})).toBe('→ execute(argv=["ls"])\n');
  });
});

describe('file-change tool call previews', () => {
  it('summarizes a single structured change as a verb and a path', () => {
    expect(toolCallPreview('file_change', {changes: [{path: 'src/lib.rs', kind: 'delete'}]})).toBe(
      '→ file_change delete src/lib.rs\n',
    );
    // The past-participle spelling maps to the same verb.
    expect(toolCallPreview('apply_patch', {changes: [{path: 'src/lib.rs', kind: 'deleted'}]})).toBe(
      '→ apply_patch delete src/lib.rs\n',
    );
  });

  it('counts the files a multi-file change touches instead of listing them all', () => {
    const preview = toolCallPreview('file_change', {
      changes: [
        {path: 'src/lib.rs', kind: 'deleted'},
        {path: 'src/queue.rs', kind: 'modified'},
        {path: 'src/new.rs', kind: 'added'},
      ],
    });

    expect(preview).toBe(
      '→ file_change 3 changes: delete src/lib.rs, modify src/queue.rs, +1 more\n',
    );
  });

  it('shows an unrecognized change kind verbatim rather than dropping it', () => {
    expect(toolCallPreview('file_change', {changes: [{path: 'a.rs', kind: 'chmod'}]})).toBe(
      '→ file_change chmod a.rs\n',
    );
    expect(toolCallPreview('file_change', {changes: [{path: 'a.rs'}]})).toBe(
      '→ file_change change a.rs\n',
    );
  });

  it('falls back to the generic rendering when changes are not an array', () => {
    expect(toolCallPreview('file_change', {changes: 'src/lib.rs'})).toBe(
      '→ file_change(changes="src/lib.rs")\n',
    );
  });
});

describe('tool call argument truncation', () => {
  it('never cuts inside a quoted JSON serialization', () => {
    const preview = toolCallPreview('Unknown', {
      spec: {path: 'a'.repeat(120), mode: 'rewrite'},
    });

    expect(preview).toBe('→ Unknown(spec={keys: path, mode})\n');
    // Every quote in the rendered line is closed.
    expect((preview.match(/"/g) ?? []).length % 2).toBe(0);
  });

  it('closes the quotes it opens around a truncated string argument', () => {
    const preview = toolCallPreview('Unknown', {text: 'x'.repeat(200)});

    expect(preview).toBe(`→ Unknown(text="${'x'.repeat(80)}...")\n`);
    expect((preview.match(/"/g) ?? []).length % 2).toBe(0);
  });

  it('summarizes an over-long array argument by its length', () => {
    const preview = toolCallPreview('Unknown', {ids: Array.from({length: 40}, (_, i) => i)});

    expect(preview).toBe('→ Unknown(ids=[40 items])\n');
  });
});

describe('collapsed typed result summaries', () => {
  it('reports exit status, wall time, and output size for a long command result', () => {
    const stdout = `${Array.from({length: 37}, (_, index) => `line ${index}`).join('\n')}\n`;
    const payload = commandPayload({stdout: stdout, stderr: '', exitCode: 1, duration: 0.44});

    const collapsed = toolResultPreview('irrelevant', payload);

    expect(collapsed.content).toBe('exit 1 · 0.4s · 37 lines');
    expect(collapsed.collapsible).toBe(true);
    expect(collapsed.hiddenLines).toBe(38);
  });

  it('separates stdout and stderr and names the exit code when expanded', () => {
    const stdout = `${Array.from({length: 8}, (_, index) => `out ${index}`).join('\n')}\n`;
    const payload = commandPayload({
      stdout: stdout,
      stderr: 'thread panicked\n',
      exitCode: 101,
      duration: 2,
    });

    const expanded = toolResultPreview('irrelevant', payload, true);

    expect(expanded.content).toStartWith('out 0\n');
    expect(expanded.content).toContain('out 7\n\nstderr:\nthread panicked\nexit code: 101');
    expect(expanded.hiddenLines).toBe(0);
  });

  it('falls back to the first meaningful line when a command reports no status', () => {
    const stdout = `\n\nfirst real line\n${'tail\n'.repeat(20)}`;
    const payload = commandPayload({stdout: stdout, stderr: ''});

    expect(toolResultPreview('irrelevant', payload).content).toBe('first real line · 23 lines');
  });

  it('shows a json result as its top-level shape while collapsed', () => {
    const value = Object.fromEntries(Array.from({length: 9}, (_, index) => [`k${index}`, index]));

    const collapsed = toolResultPreview('irrelevant', jsonPayload(value));

    expect(collapsed.content).toBe('{keys: k0, k1, k2, k3, +5 more}');
    expect(collapsed.collapsible).toBe(true);
    expect(toolResultPreview('irrelevant', jsonPayload(value), true).content).toContain('"k8": 8');
  });

  it('shows an array json result as its length', () => {
    const value = Array.from({length: 12}, (_, index) => ({index}));

    expect(toolResultPreview('irrelevant', jsonPayload(value)).content).toBe('[12 items]');
    expect(jsonShapeSummary([])).toBe('[0 items]');
    expect(jsonShapeSummary([1])).toBe('[1 item]');
    expect(jsonShapeSummary({})).toBe('{}');
  });
});

describe('elapsed labels', () => {
  it('drops to the coarsest unit the duration reaches', () => {
    expect(elapsedLabel(45_000)).toBe('45s');
    expect(elapsedLabel(65_000)).toBe('1m 5s');
    expect(elapsedLabel(3_725_000)).toBe('1h 2m');
    expect(elapsedLabel(0)).toBe('0s');
    expect(elapsedLabel(-1)).toBe('0s');
  });
});
