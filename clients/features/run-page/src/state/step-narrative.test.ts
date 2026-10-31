import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import * as fc from 'fast-check';
import {commandOutput} from './command-output.test-helper.js';
import {runPageInitial, runPageStep} from './run-page.js';
import {
  blockStarts,
  GROUP_OPEN_LIMIT,
  gateView,
  itemLines,
  narrativeOf,
  reasoningItems,
  replyOverview,
  type TurnNarrative,
  toolOf,
  turnMeta,
} from './step-narrative.js';
import type {TurnRow} from './turn-transcript.js';
import {viewedRun} from './views.js';

type ToolRow = Extract<TurnRow, {kind: 'tool'}>;

const say = (
  id: string,
  variant: 'reply' | 'reasoning' | 'prompt' | 'note',
  text: string,
): TurnRow => ({kind: 'message', id, author: 'Agent', variant, text});
const tool = (id: string, state: 'ok' | 'failed' | 'pending' = 'ok'): ToolRow => ({
  kind: 'tool',
  id,
  name: 'run',
  state,
  input: '{}',
  output: state === 'pending' ? null : 'out',
});
const reply: TurnRow = {
  kind: 'reply',
  id: 'r',
  role: null,
  author: 'Agent',
  replyKind: 'Result',
  title: 'Result',
  fields: [],
  value: {},
  raw: '{}',
};

const shape = (narrative: TurnNarrative): string[] =>
  narrative.blocks.map(block => `${block.kind}:${block.id}`);

describe('an agent turn as narrative', () => {
  test('keeps thinking and text in time order, with the structured reply last', () => {
    const narrative = narrativeOf([
      say('a', 'reasoning', 'hmm'),
      say('b', 'reply', 'I will pad'),
      reply,
      say('c', 'reasoning', 'ok'),
    ]);
    expect(shape(narrative)).toEqual(['thought:a', 'said:b', 'thought:c', 'reply:r']);
  });

  test('folds consecutive tool calls into one group, and text splits groups', () => {
    const narrative = narrativeOf([
      tool('t1'),
      tool('t2', 'failed'),
      say('a', 'reply', 'then'),
      tool('t3'),
    ]);
    expect(shape(narrative)).toEqual(['tools:t1', 'said:a', 'tools:t3']);
    expect(narrative.tools).toBe(3);
    expect(narrative.failed).toBe(1);
    const first = narrative.blocks[0];
    expect(first?.kind === 'tools' ? first.tools.map(each => each.id) : []).toEqual(['t1', 't2']);
  });

  test('a prompt or an empty message between tool calls does not split their group', () => {
    const narrative = narrativeOf([
      tool('t1'),
      say('p', 'prompt', 'the task'),
      say('e', 'reply', '   '),
      tool('t2'),
    ]);
    expect(shape(narrative)).toEqual(['tools:t1']);
    expect(narrative.tools).toBe(2);
  });

  test('says a failed note and drops an ordinary one', () => {
    const failure: TurnRow = {...say('n', 'note', 'boom'), tone: 'err'} as TurnRow;
    const narrative = narrativeOf([say('ok', 'note', 'fine'), failure]);
    expect(narrative.blocks).toEqual([{kind: 'said', id: 'n', text: 'Agent: boom'}]);
  });

  test('finds the group that holds a tool call', () => {
    const narrative = narrativeOf([tool('t1'), say('a', 'reply', 'x'), tool('t2')]);
    expect(toolOf(narrative, 't2')?.block).toBe('t2');
    expect(toolOf(narrative, 'nope')).toBeNull();
  });

  test('a long generated turn keeps every tool call once and in order', () => {
    let seed = 7;
    const next = (): number => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed;
    };
    for (let round = 0; round < 50; round++) {
      const rows: TurnRow[] = Array.from({length: 300}, (_, index) => {
        const pick = next() % 4;
        if (pick === 0) return say(`m${index}`, 'reply', 'x');
        if (pick === 1) return say(`m${index}`, 'reasoning', 'x');
        return tool(`t${index}`, next() % 7 === 0 ? 'failed' : 'ok');
      });
      const narrative = narrativeOf(rows);
      const tools = narrative.blocks.flatMap(block => (block.kind === 'tools' ? block.tools : []));
      expect(tools.map(each => each.id)).toEqual(
        rows.flatMap(row => (row.kind === 'tool' ? [row.id] : [])),
      );
      expect(new Set(shape(narrative)).size).toBe(narrative.blocks.length);
    }
  });

  test('whatever the order of the rows, the replies come last, in the order they arrived', () => {
    let seed = 11;
    const next = (): number => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed;
    };
    for (let round = 0; round < 50; round++) {
      const rows: TurnRow[] = Array.from({length: 40}, (_, index) => {
        const pick = next() % 5;
        if (pick === 0) return {...reply, id: `r${index}`};
        if (pick === 1) return say(`m${index}`, 'reasoning', 'x');
        return tool(`t${index}`);
      });
      const kinds = narrativeOf(rows).blocks.map(block => block.kind);
      const firstReply = kinds.indexOf('reply');
      if (firstReply >= 0)
        expect(kinds.slice(firstReply).every(kind => kind === 'reply')).toBe(true);
      const replies = narrativeOf(rows).blocks.flatMap(block =>
        block.kind === 'reply' ? [block.id] : [],
      );
      expect(replies).toEqual(rows.flatMap(row => (row.kind === 'reply' ? [row.id] : [])));
    }
  });

  test('the header meta reads duration, tools and failures', () => {
    const narrative = narrativeOf([tool('a'), tool('b', 'failed')]);
    expect(turnMeta(100, narrative)).toBe('1m · 2 tools · 1 failed');
    expect(turnMeta(null, narrativeOf([tool('a')]))).toBe('1 tool');
  });
});

describe('reasoning items', () => {
  const TITLE_A = '**Inspecting repository history**';
  const TITLE_B = '**Scoping the change**';

  test('items glued into one entry are split at their bold titles', () => {
    expect(
      reasoningItems(`${TITLE_A}\n\nLooking at git log.${TITLE_B}\n\nNarrow it down.`),
    ).toEqual([`${TITLE_A}\n\nLooking at git log.`, `${TITLE_B}\n\nNarrow it down.`]);
    expect(reasoningItems(`${TITLE_A} ${TITLE_B}`)).toEqual([TITLE_A, TITLE_B]);
    expect(reasoningItems(`${TITLE_A}\n\none\n\n${TITLE_B}\n\ntwo`)).toHaveLength(2);
  });

  test('a bold phrase inside a sentence, or text with no title, is one item', () => {
    expect(reasoningItems('Use the **ring** buffer here.')).toEqual([
      'Use the **ring** buffer here.',
    ]);
    expect(reasoningItems('plain thinking')).toEqual(['plain thinking']);
    expect(reasoningItems(`${TITLE_A}\n\nsee **this**`)).toHaveLength(1);
  });

  test('no text is lost or reordered whatever the items', () => {
    const squash = (text: string): string => text.replace(/\s+/g, '');
    for (const count of [1, 2, 5, 9]) {
      const text = Array.from({length: count}, (_, n) => `**Title ${n}**\n\nBody ${n}.`).join('');
      const items = reasoningItems(text);
      expect(items).toHaveLength(count);
      expect(squash(items.join(''))).toBe(squash(text));
    }
  });

  test('the narrative has one thought per item, ids stable as the entry grows', () => {
    const first = say('r', 'reasoning', `${TITLE_A}\n\nOne.`);
    const grown = say('r', 'reasoning', `${TITLE_A}\n\nOne.${TITLE_B}\n\nTwo.`);
    expect(shape(narrativeOf([first]))).toEqual(['thought:r']);
    expect(shape(narrativeOf([grown]))).toEqual(['thought:r', 'thought:r#1']);
  });
});

describe('a structured reply at first', () => {
  const field = (kind: 'prose' | 'fact', key: string): never =>
    ({kind, key, label: key, text: 'x', value: 'x'}) as never;

  test('shows its summary, else its first prose field, and nothing else', () => {
    expect(
      replyOverview([
        field('fact', 'handle'),
        field('prose', 'details'),
        field('prose', 'summary'),
      ]),
    ).toEqual(['summary']);
    expect(replyOverview([field('fact', 'handle'), field('prose', 'analysis')])).toEqual([
      'analysis',
    ]);
    expect(replyOverview([field('fact', 'handle')])).toEqual([]);
  });
});

describe('one tool call in full', () => {
  test('reads input, then result, and an error in the error stream', () => {
    const ok = tool('t');
    expect(itemLines(ok).map(line => line.text)).toEqual(['Input', '{}', '', 'Result', 'out']);
    const bad = {...ok, state: 'failed' as const};
    expect(
      itemLines(bad)
        .filter(line => line.stream === 'err')
        .map(line => line.text),
    ).toEqual(['out']);
    expect(itemLines({...ok, input: null, output: null})).toEqual([]);
  });
});

function span(id: string, parent: string | null, patch: Partial<Span>): Span {
  return {
    id,
    parent,
    type: parent === null ? 'flow' : 'operation',
    name: id,
    status: 'done',
    started_at: 1,
    ...patch,
  } as Span;
}

const GATE = span('op:gate', 'flow:root#1', {
  label: {
    kind: 'gate',
    title: 'benchmark',
    verdict: 'failed_check',
    attrs_json: JSON.stringify({
      stage: 'benchmark',
      metric_value: 13318502.55,
      vs_baseline: 5.72,
      failed: {check: 'accuracy', completed: 93, required: 100, unit: 'trials'},
    }),
    links: [],
  },
} as Partial<Span>);

function run(events: readonly RunEvent[]) {
  let state = runPageInitial({
    identity: {host: 'ssh:host-b', runId: '20261015-163047'},
    title: {project: '~/p', task: 'mpmc'},
    hostLabel: 'host-b',
    unavailable: {},
  });
  state = {...state, link: {kind: 'attached', instance: 'a1b2c3d4e5f6'}};
  state = runPageStep(state, {
    type: 'run-data',
    wallMs: 0,
    data: {
      kind: 'events',
      events: [span('flow:root#1', null, {name: 'multi'}), GATE].map((each, index) => ({
        ...event(index + 1, 'span_started'),
        data: {kind: 'span_started', span: each},
      })) as RunEvent[],
    },
  }).state;
  const viewed = viewedRun(state);
  return {trace: viewed.trace, core: reduceEventBatch(initialCoreState(), events)};
}

describe('a gate', () => {
  test('reads the verdict first, then value, ratios and how far the failed check came', () => {
    const {trace, core} = run([]);
    const view = gateView(trace, core, 'op:gate', 9_500_000);
    expect(view?.verdict).toEqual({label: 'Failed a check', tone: 'err'});
    expect(view?.numbers).toEqual([
      ['value', '13,318,503'],
      ['vs input', '5.72×'],
      ['vs parent', '1.40×'],
      ['failed check', 'Accuracy · 93/100 trials'],
    ]);
  });

  test('has no ratio to a parent the run did not measure, and a missing step is none', () => {
    const {trace, core} = run([]);
    expect(gateView(trace, core, 'op:gate', null)?.numbers.map(row => row[0])).not.toContain(
      'vs parent',
    );
    expect(gateView(trace, core, 'op:missing', null)).toBeNull();
  });

  test('its log is what the run recorded under its span, standard error in the error stream', () => {
    const {trace, core} = run([
      event(10, 'subprocess_output', {
        span_id: 'op:gate',
        data: {
          kind: 'subprocess_output',
          process_id: 'p1',
          process_kind: 'check',
          stream: 'stdout',
          content: 'accuracy: 93/100\nretrying',
        },
      }),
      event(11, 'subprocess_output', {
        span_id: 'op:gate',
        data: {
          kind: 'subprocess_output',
          process_id: 'p1',
          process_kind: 'check',
          stream: 'stderr',
          content: 'warn',
        },
      }),
    ]);
    expect(gateView(trace, core, 'op:gate', null)?.lines).toEqual([
      {text: 'accuracy: 93/100', stream: 'out'},
      {text: 'retrying', stream: 'out'},
      {text: 'warn', stream: 'err'},
    ]);
  });
});

describe('how thoughts and tool groups start', () => {
  const TURN_ROWS = fc.array(
    fc.oneof(
      fc.constant('thought' as const),
      fc.constant('said' as const),
      fc.constant('tool' as const),
      fc.constant('failed' as const),
      fc.constant('reply' as const),
    ),
    {maxLength: 60},
  );

  const rowsOf = (kinds: readonly string[]): TurnRow[] =>
    kinds.map((kind, index) => {
      if (kind === 'thought') return say(`m${index}`, 'reasoning', `thought ${index}`);
      if (kind === 'said') return say(`m${index}`, 'reply', `said ${index}`);
      if (kind === 'reply') return {...reply, id: `r${index}`};
      return tool(`t${index}`, kind === 'failed' ? 'failed' : 'ok');
    });

  test('a finished turn starts every thought closed, whatever its blocks', () => {
    fc.assert(
      fc.property(TURN_ROWS, kinds => {
        const starts = blockStarts(narrativeOf(rowsOf(kinds)), false);
        for (const [id, start] of starts) {
          expect(start.live, id).toBe(false);
          if (id.startsWith('m')) expect(start.open, id).toBe(false);
        }
      }),
    );
  });

  test('a live turn has at most one thought open as live, and only the one it ends with', () => {
    fc.assert(
      fc.property(TURN_ROWS, kinds => {
        const narrative = narrativeOf(rowsOf(kinds));
        const starts = blockStarts(narrative, true);
        const live = [...starts].filter(([, start]) => start.live);
        const last = narrative.blocks.at(-1);
        if (last?.kind === 'thought') {
          expect(live.map(([id]) => id)).toEqual([last.id]);
          expect(starts.get(last.id)?.open).toBe(true);
        } else {
          expect(live).toEqual([]);
        }
        for (const [id, start] of starts) {
          if (id.startsWith('m') && !start.live) expect(start.open, id).toBe(false);
        }
      }),
    );
  });

  test('a group starts open exactly when it holds no more than the limit', () => {
    fc.assert(
      fc.property(TURN_ROWS, fc.boolean(), (kinds, live) => {
        const narrative = narrativeOf(rowsOf(kinds));
        const starts = blockStarts(narrative, live);
        for (const block of narrative.blocks) {
          if (block.kind !== 'tools') continue;
          expect(starts.get(block.id), block.id).toEqual({
            live: false,
            open: block.tools.length <= GROUP_OPEN_LIMIT,
          });
        }
      }),
    );
  });

  test('says nothing of blocks that are neither a thought nor a group', () => {
    const narrative = narrativeOf([say('a', 'reply', 'x'), reply]);
    expect([...blockStarts(narrative, true).keys()]).toEqual([]);
  });
});

describe('a command in the item pane', () => {
  const command = (patch: Partial<NonNullable<ToolRow['command']>> = {}): ToolRow => ({
    ...tool('t', 'ok'),
    input: '{"command":"ls"}',
    output: 'a\nb',
    command: {exitCode: 0, seconds: 1.2, stdout: 'a\nb', stderr: '', ...patch},
  });
  const titles = (lines: readonly {text: string}[]): string[] =>
    lines.map(line => line.text).filter(text => ['Input', 'Stdout', 'Stderr'].includes(text));

  test('reads input, then stdout, then stderr, stderr in the error color', () => {
    const lines = itemLines(command({stderr: 'warn'}));
    expect(titles(lines)).toEqual(['Input', 'Stdout', 'Stderr']);
    expect(lines.filter(line => line.stream === 'err').map(line => line.text)).toEqual(['warn']);
    expect(lines.filter(line => line.text === '')).toHaveLength(2);
  });

  test('leaves out a stream with nothing in it, and has no "Result" title of its own', () => {
    expect(titles(itemLines(command()))).toEqual(['Input', 'Stdout']);
    expect(titles(itemLines(command({stdout: '', stderr: 'x'})))).toEqual(['Input', 'Stderr']);
    expect(titles(itemLines(command({stdout: '', stderr: ''})))).toEqual(['Input']);
    for (const patch of [{}, {stderr: 'x'}, {stdout: ''}]) {
      expect(itemLines(command(patch)).map(line => line.text)).not.toContain('Result');
    }
  });

  test('shows no blank line for the newline that ends a stream', () => {
    const lines = itemLines(command({stdout: 'a\nb\n', stderr: 'warn\n'}));
    expect(lines.map(line => line.text)).toEqual([
      'Input',
      '{"command":"ls"}',
      '',
      'Stdout',
      'a',
      'b',
      '',
      'Stderr',
      'warn',
    ]);
  });

  test('both streams, with every line of each kept once and in order, whatever they hold', () => {
    const lineBreak = /\r?\n/;
    fc.assert(
      fc.property(commandOutput, commandOutput, (stdout, stderr) => {
        const shown = itemLines(command({stdout, stderr}));
        const body = (text: string): string[] => text.replace(/\r?\n$/, '').split(lineBreak);
        const out = body(stdout);
        const err = body(stderr);
        const hasOut = stdout.replace(/\r?\n$/, '') !== '';
        const hasErr = stderr.replace(/\r?\n$/, '') !== '';
        // The sections in order, a blank line between them: the whole pane.
        const sections = [
          ['Input', '{"command":"ls"}'],
          ...(hasOut ? [['Stdout', ...out]] : []),
          ...(hasErr ? [['Stderr', ...err]] : []),
        ];
        expect(shown.map(line => line.text)).toEqual(
          sections.flatMap((section, index) => (index === 0 ? section : ['', ...section])),
        );
        // Whatever the text, the error color is stderr's lines and only those.
        expect(shown.filter(line => line.stream === 'err').map(line => line.text)).toEqual(
          hasErr ? err : [],
        );
        // Both streams are there whenever both hold something.
        expect(titles(shown)).toEqual([
          'Input',
          ...(hasOut ? ['Stdout'] : []),
          ...(hasErr ? ['Stderr'] : []),
        ]);
        // No line carries a break of its own.
        expect(shown.some(line => lineBreak.test(line.text))).toBe(false);
      }),
    );
  });

  test('keeps both streams of a command that wrote to each, in a CRLF output too', () => {
    const lines = itemLines(command({stdout: 'a\r\nb\r\n', stderr: 'warn\r\n'}));
    expect(lines.map(line => line.text)).toEqual([
      'Input',
      '{"command":"ls"}',
      '',
      'Stdout',
      'a',
      'b',
      '',
      'Stderr',
      'warn',
    ]);
  });
});
