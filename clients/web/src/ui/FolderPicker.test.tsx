import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {FolderPicker} from './FolderPicker.js';

const none = () => undefined;

test('the picker lists subfolders with their git tag, goes up, and chooses the folder it shows', () => {
  const html = renderToStaticMarkup(
    <FolderPicker
      listing={{
        path: '/Users/me/src',
        parent: '/Users/me',
        entries: [
          {name: 'llm-serve', path: '/Users/me/src/llm-serve', git: true},
          {name: 'notes', path: '/Users/me/src/notes', git: false},
        ],
      }}
      error={null}
      onOpen={none}
      onChoose={none}
      onClose={none}
    />,
  );
  assert.match(
    html,
    /<dialog class="picker" aria-labelledby="picker-title"><h4 id="picker-title" class="mono" title="\/Users\/me\/src">\/Users\/me\/src<\/h4>/,
  );
  assert.match(html, /aria-label="Up"/);
  assert.match(
    html,
    /title="\/Users\/me\/src\/llm-serve">.*<span class="nm">llm-serve<\/span><span class="kbd">git<\/span><\/button>/,
  );
  assert.match(html, /<span class="nm">notes<\/span><\/button>/);
  assert.match(html, /<button type="button" class="btn primary">Choose this folder<\/button>/);
});

test('at the roots there is nothing to choose; errors show under the list', () => {
  const html = renderToStaticMarkup(
    <FolderPicker
      listing={{path: null, parent: null, entries: [{name: 'me', path: '/Users/me', git: false}]}}
      error="Path is outside the granted roots"
      onOpen={none}
      onChoose={none}
      onClose={none}
    />,
  );
  assert.match(html, />Folders you can open<\/h4>/);
  assert.doesNotMatch(html, /aria-label="Up"/);
  assert.match(html, /<p class="hint bad" role="alert">Path is outside the granted roots<\/p>/);
  assert.match(html, /class="btn primary" disabled="">Choose this folder/);
  assert.match(
    renderToStaticMarkup(
      <FolderPicker listing={null} error={null} onOpen={none} onChoose={none} onClose={none} />,
    ),
    />Loading…</,
  );
});
