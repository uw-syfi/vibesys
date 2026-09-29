import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {Composer} from './Composer.js';

test('a held composer keeps typing open but waits to send; extra controls sit before Send', () => {
  const html = renderToStaticMarkup(
    <Composer
      id="ask"
      label="Ask about this run"
      placeholder="Ask about this run…"
      draft="And round 4?"
      onDraft={() => {}}
      disabled={false}
      held
      error={null}
      onSend={async () => true}
    >
      <button type="button" className="mchip">
        claude-opus-5
      </button>
    </Composer>,
  );
  assert.match(html, /<input id="ask" aria-label="Ask about this run"[^>]*value="And round 4\?"/);
  assert.doesNotMatch(html, /<input[^>]*disabled/);
  assert.match(
    html,
    /class="mchip">claude-opus-5<\/button><button type="button" class="send" aria-label="Send" title="Waiting for the answer" disabled="">/,
  );
});
