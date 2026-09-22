import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {Log} from './Log.js';

const note = (round: number | null) =>
  renderToStaticMarkup(
    <Log
      state="ready"
      round={round}
      groups={[]}
      follow={false}
      history={{loading: false, error: null, onRetry: () => {}}}
    />,
  );

test('empty log note: the baseline has no agent calls; other rounds have none yet', () => {
  assert.match(note(0), /No agent calls in the baseline/);
  assert.match(note(3), /No agent calls in this round yet/);
  assert.match(note(null), /No round has started yet/);
});
