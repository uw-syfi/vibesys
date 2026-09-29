import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {AskView, ThreadRow} from '../ask.js';
import {AskTab, type AskTabProps} from './Ask.js';

const row = (id: string, title: string, count: number): ThreadRow => ({
  id,
  title,
  provider: 'claude',
  model: 'claude-opus-5',
  count,
});
const VIEW: AskView = {
  harness: 'available',
  threads: [row('default', 'Why did round 3 fail the judge?', 2), row('t2', 'New thread', 0)],
  current: row('default', 'Why did round 3 fail the judge?', 2),
  messages: [
    {
      id: 'chat-5',
      question: 'Why did round 3 fail the judge?',
      answer: [[{kind: 'text', text: 'On correctness, not performance.'}]],
      error: null,
    },
    {id: 'ask-2', question: 'And round 4?', answer: null, error: null},
  ],
  pending: true,
  streaming: null,
  groups: [
    {
      provider: 'claude',
      label: 'Claude Code harness',
      models: ['claude-opus-5', 'claude-sonnet-5'],
    },
  ],
};
const props = (overrides: Partial<AskTabProps> = {}): AskTabProps => ({
  view: VIEW,
  menu: null,
  draft: '',
  reason: null,
  error: null,
  onMenu: () => {},
  onThread: () => {},
  onNewThread: () => {},
  onDraft: () => {},
  onSend: async () => true,
  onRetry: () => {},
  ...overrides,
});

test('a thread: its title opens the switcher, questions and answers, the pending one answering, the model chip', () => {
  const html = renderToStaticMarkup(<AskTab {...props()} />);
  assert.match(html, /aria-label="Thread: Why did round 3 fail the judge\?"/);
  assert.match(html, /aria-label="New thread"/);
  assert.match(html, /<div class="human">Why did round 3 fail the judge\?<\/div>/);
  assert.match(
    html,
    /<div class="who2">claude-opus-5<\/div><p>On correctness, not performance\.<\/p>/,
  );
  assert.match(html, /Answering…/);
  assert.match(html, /aria-label="Chat model"[^>]*>claude-opus-5/);
  assert.match(html, /class="send" aria-label="Send" title="Waiting for the answer" disabled=""/);
});

test('menus: threads with their counts, models grouped by harness with the current one checked', () => {
  const threads = renderToStaticMarkup(<AskTab {...props({menu: 'thread'})} />);
  assert.match(threads, /<div class="gh">2 threads<\/div>/);
  // The head's + is the one New thread control.
  assert.doesNotMatch(threads, /role="menuitem"|class="sepl"/);
  assert.match(
    threads,
    /role="menuitemradio" aria-checked="true" aria-label="Why did round 3 fail the judge\?, 2 questions"/,
  );
  const models = renderToStaticMarkup(<AskTab {...props({menu: 'model'})} />);
  assert.match(models, /<div class="gh">Claude Code harness<\/div>/);
  assert.match(models, /aria-checked="true" class="it on">claude-opus-5/);
  assert.match(models, /aria-checked="false" class="it">claude-sonnet-5/);
});

test('without a chat harness the tab says so; recorded threads stay readable', () => {
  const empty = row('default', 'New thread', 0);
  const none: AskView = {
    ...VIEW,
    harness: 'none',
    threads: [empty],
    current: empty,
    messages: [],
    pending: false,
  };
  const html = renderToStaticMarkup(<AskTab {...props({view: none})} />);
  assert.match(html, /This run offers no chat harness\./);
  assert.doesNotMatch(html, /Ask about this run/);
  const history = renderToStaticMarkup(
    <AskTab {...props({view: {...VIEW, harness: 'none', pending: false}})} />,
  );
  assert.match(history, /class="human"/);
  assert.match(history, /This run offers no chat harness\./);
  assert.doesNotMatch(history, /aria-label="New thread"/);
});

test('an options query that failed says so with a Retry; history keeps the status line', () => {
  const empty = row('default', 'New thread', 0);
  const failed: AskView = {
    ...VIEW,
    harness: 'failed',
    threads: [empty],
    current: empty,
    messages: [],
    pending: false,
  };
  const html = renderToStaticMarkup(<AskTab {...props({view: failed})} />);
  assert.match(html, /Couldn’t check the chat harness\./);
  assert.match(html, /<button type="button" class="linkish"[^>]*>Retry<\/button>/);
  const history = renderToStaticMarkup(
    <AskTab {...props({view: {...VIEW, harness: 'checking', pending: false}})} />,
  );
  assert.match(history, /class="human"/);
  assert.match(history, /Checking the chat harness…/);
  assert.doesNotMatch(history, /Ask about this run/);
});
