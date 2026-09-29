import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {SteerComposer} from './SteerComposer.js';

test('the composer states why it is off, and shows a failed steer', () => {
  const html = renderToStaticMarkup(
    <SteerComposer
      disabled
      reason="Steering resumes when the connection returns"
      error="Steer failed: gateway closed"
      draft=""
      onDraft={() => {}}
      onSend={async () => true}
    />,
  );
  assert.match(html, /class="pill off"/);
  assert.match(html, /placeholder="Steering resumes when the connection returns"/);
  assert.match(html, /role="alert">Steer failed: gateway closed</);
  assert.match(html, /<input id="steer"/);
});
