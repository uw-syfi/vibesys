import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {ChangesModel, FileChanges} from '../changes.js';
import {Changes} from './Changes.js';

const file = (path: string, body: FileChanges['body']): FileChanges => ({
  path,
  renamedFrom: null,
  change: 'modified',
  added: body.kind === 'patch' ? 1 : 0,
  removed: 0,
  body,
  command: `git diff b h -- ${path}`,
});

test('unavailable and truncated patches offer the reproduction command; loading says so', () => {
  const model: ChangesModel = {
    kind: 'files',
    round: 3,
    against: 'the round 2 checkpoint',
    commit: 'c0ffee03',
    files: [
      file('src/a.rs', {kind: 'unavailable'}),
      file('src/b.rs', {
        kind: 'patch',
        lines: [{tone: 'add', text: 'x', line: 1}],
        truncated: true,
      }),
      file('src/c.rs', {kind: 'loading'}),
    ],
  };
  const html = renderToStaticMarkup(<Changes model={model} copied={null} onCopy={() => {}} />);
  assert.match(
    html,
    /<span class="scope">Round 3<\/span><span>against the round 2 checkpoint<\/span>/,
  );
  assert.match(html, /The workspace repository could not produce this patch\./);
  assert.match(html, /Copy <span class="mono">git diff b h -- src\/a.rs<\/span>/);
  assert.match(html, /Patch truncated at the server&#x27;s size bound\./);
  assert.match(html, /<div class="add">/);
  assert.match(html, /Loading patch…/);
});

test('a running round and a round without a recorded range say why there is nothing', () => {
  const running = renderToStaticMarkup(
    <Changes model={{kind: 'running', round: 6}} copied={null} onCopy={() => {}} />,
  );
  assert.match(running, /Changes appear when round 6 finishes\./);
  const none = renderToStaticMarkup(
    <Changes model={{kind: 'unresolved', round: 3}} copied={null} onCopy={() => {}} />,
  );
  assert.match(none, /No change range was recorded for round 3\./);
});
