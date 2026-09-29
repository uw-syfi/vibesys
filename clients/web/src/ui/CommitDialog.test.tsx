import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {CommitDialog} from './CommitDialog.js';

const none = () => undefined;

test('the confirmation lists exactly the task files and counts what stays uncommitted', () => {
  const html = renderToStaticMarkup(
    <CommitDialog
      preview={{
        task_files: ['.vibesys/tasks/d/OBJECTIVE.md', '.vibesys/tasks/d/vibesys.input.toml'],
        other: ['src/a.rs'],
      }}
      error={null}
      busy={false}
      onCommit={none}
      onClose={none}
    />,
  );
  assert.match(html, /<h4 id="commit-title">Commit the task files\?<\/h4>/);
  assert.match(
    html,
    /<ul class="files mono"><li>.vibesys\/tasks\/d\/OBJECTIVE.md<\/li><li>.vibesys\/tasks\/d\/vibesys.input.toml<\/li><\/ul>/,
  );
  assert.match(
    html,
    /<p class="t2" title="src\/a.rs">1 other changed file stays uncommitted\.<\/p>/,
  );
  assert.match(html, /<button type="button" class="btn primary">Commit<\/button>/);
});

test('nothing to commit until the preview loads; a failure shows its stderr', () => {
  const loading = renderToStaticMarkup(
    <CommitDialog preview={null} error={null} busy={false} onCommit={none} onClose={none} />,
  );
  assert.match(loading, />Loading…</);
  assert.match(loading, /class="btn primary" disabled="">Commit/);
  const failed = renderToStaticMarkup(
    <CommitDialog
      preview={{task_files: ['x'], other: []}}
      error={'git commit failed\nhook rejected'}
      busy={false}
      onCommit={none}
      onClose={none}
    />,
  );
  assert.match(failed, /<pre class="err" role="alert">git commit failed\nhook rejected<\/pre>/);
});
