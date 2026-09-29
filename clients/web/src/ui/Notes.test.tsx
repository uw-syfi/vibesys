import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {NoteState} from '../notes.js';
import {NotesTab, type NotesTabProps} from './Notes.js';

const READY: NoteState = {
  phase: 'ready',
  runId: 'r1',
  text: 'Check p99.',
  saved: 'Check p99.',
  error: null,
};
const props = (overrides: Partial<NotesTabProps> = {}): NotesTabProps => ({
  note: READY,
  canSteer: true,
  harness: 'available',
  onEdit: () => {},
  onBlur: () => {},
  onRetry: () => {},
  onSteerDraft: () => {},
  onAskDraft: () => {},
  ...overrides,
});

test('the editor with its scope line and both draft buttons', () => {
  const html = renderToStaticMarkup(<NotesTab {...props()} />);
  assert.match(html, /<span class="scope">Run<\/span><span>never sent to agents<\/span>/);
  assert.match(html, /<textarea class="notesed" aria-label="Notes"[^>]*>Check p99\.<\/textarea>/);
  assert.match(
    html,
    /title="Put this note in the steer composer; nothing is sent">Use as steer draft</,
  );
  assert.match(
    html,
    /title="Put this note in the Ask composer; nothing is sent">Use as ask draft</,
  );
});

test('empty or ended: the draft buttons are off and say why on hover; a failed save is shown', () => {
  const empty = renderToStaticMarkup(<NotesTab {...props({note: {...READY, text: ' '}})} />);
  assert.equal(empty.match(/disabled=""/g)?.length, 2);
  const ended = renderToStaticMarkup(
    <NotesTab {...props({canSteer: false, harness: 'none', note: {...READY, error: 'offline'}})} />,
  );
  assert.match(ended, /disabled="" title="The run has ended">Use as steer draft/);
  assert.match(ended, /disabled="" title="This run offers no chat harness">Use as ask draft/);
  const checking = renderToStaticMarkup(<NotesTab {...props({harness: 'checking'})} />);
  assert.match(checking, /disabled="" title="Checking the chat harness…">Use as ask draft/);
  const failed = renderToStaticMarkup(<NotesTab {...props({harness: 'failed'})} />);
  assert.match(failed, /disabled="" title="Couldn’t check the chat harness">Use as ask draft/);
  assert.match(ended, /role="alert">Not saved: offline</);
});

test('loading, failed with Retry, and no home server', () => {
  assert.match(
    renderToStaticMarkup(<NotesTab {...props({note: {phase: 'loading', runId: 'r1'}})} />),
    /Loading…/,
  );
  const failed = renderToStaticMarkup(
    <NotesTab {...props({note: {phase: 'failed', runId: 'r1', message: 'HTTP 500'}})} />,
  );
  assert.match(failed, /role="alert" title="HTTP 500">Couldn’t load notes\. <button[^>]*>Retry</);
  assert.doesNotMatch(failed, /never sent to agents/);
  assert.doesNotMatch(failed, /textarea/);
  assert.match(
    renderToStaticMarkup(<NotesTab {...props({note: {phase: 'unavailable'}})} />),
    /Notes are kept by the VibeSys home server/,
  );
});
