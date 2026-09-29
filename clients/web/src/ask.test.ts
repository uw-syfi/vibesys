import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import type {ChatOptions, RunEvent} from '@vibesys/backend-client';
import {type ChatThread, DEFAULT_CHAT_THREAD_ID, type TranscriptEntry} from '@vibesys/core-state';
import {type AskInput, askView} from './ask.js';
import type {SentAsk} from './session.js';

const chat = (
  sequence: number,
  question: string,
  answer: string,
  thread: string | null = null,
): RunEvent => ({
  sequence,
  type: 'chat',
  timestamp: '2026-09-25T14:00:00Z',
  text: question,
  status: 'answered',
  agent_kind: 'chat',
  round_label: 'experiment-chat',
  chat_thread_id: thread,
  data: {kind: 'chat', answer, invocation_id: `inv-${sequence}`},
});
const DEFAULT: ChatThread = {
  id: DEFAULT_CHAT_THREAD_ID,
  title: '',
  driver: null,
  provider: null,
  model: null,
};
const SONNET: ChatThread = {
  id: 't2',
  title: '',
  driver: 'agentshim',
  provider: 'claude',
  model: 'claude-sonnet-5',
};
const OPTIONS: ChatOptions = {
  providers: [
    {
      provider: 'claude',
      models: [
        {model: 'claude-opus-5', source: 'run', default: true},
        {model: 'claude-sonnet-5', source: 'suggested'},
      ],
    },
    {provider: 'gemini', models: []},
  ],
};
const ask = (
  id: string,
  threadId: string,
  text: string,
  fields: Partial<SentAsk> = {},
): SentAsk => ({
  id,
  threadId,
  text,
  afterSequence: 10,
  answer: null,
  error: null,
  ...fields,
});
const BASE: AskInput = {
  threads: [DEFAULT, SONNET],
  transcripts: {},
  captured: [],
  asks: [],
  options: OPTIONS,
  checking: false,
  selected: DEFAULT_CHAT_THREAD_ID,
};

test('threads: questions and answers per thread, titles from the first question, the implicit thread on the run default', () => {
  const view = askView({
    ...BASE,
    captured: [
      chat(11, 'Why did round 3 fail the judge?', 'On correctness.'),
      chat(12, 'What changed in round 5?\nIn detail.', 'Prefetch.', 't2'),
    ],
  });
  assert.deepEqual(
    view.threads.map(row => [row.id, row.title, row.model, row.count]),
    [
      ['default', 'Why did round 3 fail the judge?', 'claude-opus-5', 1],
      ['t2', 'What changed in round 5?', 'claude-sonnet-5', 1],
    ],
  );
  assert.deepEqual(
    view.messages.map(message => [message.question, message.answer]),
    [['Why did round 3 fail the judge?', [[{kind: 'text', text: 'On correctness.'}]]]],
  );
  assert.equal(view.harness, 'available');
  assert.deepEqual(view.groups, [
    {
      provider: 'claude',
      label: 'Claude Code harness',
      models: ['claude-opus-5', 'claude-sonnet-5'],
    },
  ]);
});

test('a pending question holds its thread and shows the streamed answer; other threads stay free', () => {
  const streamed: TranscriptEntry = {
    id: 'e1',
    kind: 'assistant',
    content: 'Looking at the verdict',
    turnId: 'inv-9',
  };
  const input: AskInput = {
    ...BASE,
    asks: [ask('ask-1', 'default', 'Why?')],
    transcripts: {default: [streamed]},
  };
  const view = askView(input);
  assert.equal(view.pending, true);
  assert.deepEqual(
    view.messages.map(message => [message.question, message.answer, message.error]),
    [['Why?', null, null]],
  );
  assert.deepEqual(view.streaming, [[{kind: 'text', text: 'Looking at the verdict'}]]);
  const other = askView({...input, selected: 't2'});
  assert.deepEqual([other.pending, other.messages.length, other.streaming], [false, 0, null]);
});

test('an answer recorded on the stream before its response shows once; unrecorded answers and failures stay', () => {
  const both = askView({
    ...BASE,
    asks: [ask('ask-1', 'default', 'Why?')],
    captured: [chat(11, 'Why?', 'Because.')],
  });
  assert.deepEqual(
    both.messages.map(message => message.id),
    ['chat-11'],
  );
  assert.equal(both.pending, true, 'Send waits for the response all the same');
  const older = askView({
    ...BASE,
    asks: [ask('ask-1', 'default', 'Why?')],
    captured: [chat(9, 'Why?', 'Earlier.')],
  });
  assert.deepEqual(
    older.messages.map(message => message.id),
    ['chat-9', 'ask-1'],
    'an earlier identical question is not this one',
  );
  const local = askView({
    ...BASE,
    selected: 't2',
    asks: [
      ask('ask-2', 't2', 'Hi', {answer: 'Thread t2 cannot answer right now.'}),
      ask('ask-3', 't2', 'Again', {error: 'gateway closed'}),
    ],
  });
  assert.deepEqual(
    local.messages.map(message => [message.question, message.answer !== null, message.error]),
    [
      ['Hi', true, null],
      ['Again', false, 'gateway closed'],
    ],
  );
  assert.equal(local.pending, false);
});

test('a thread created a moment ago is current before its record arrives', () => {
  const view = askView({...BASE, selected: 't9'});
  assert.deepEqual(
    [view.current.id, view.current.title, view.current.model],
    ['t9', 'New thread', 'claude-opus-5'],
  );
  assert.deepEqual(
    view.threads.map(row => row.id),
    ['default', 't2', 't9'],
  );
});

test('the harness: available with a provider, checking until the options query answers, none otherwise', () => {
  assert.equal(askView({...BASE, options: null, checking: true}).harness, 'checking');
  assert.equal(askView({...BASE, options: null}).harness, 'none');
  assert.equal(askView({...BASE, options: {providers: []}}).harness, 'none');
  assert.equal(askView({...BASE, options: null}).current.model, null);
});
