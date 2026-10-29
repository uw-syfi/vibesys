import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {runPageInitial, runPageStep} from './run-page.js';
import {
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
