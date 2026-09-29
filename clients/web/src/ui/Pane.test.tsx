import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {Pane, Placeholder} from './Pane.js';

test('the pane: five tabs, the selected one marked, a close button, a keyboard-resizable edge', () => {
  const html = renderToStaticMarkup(
    <Pane tab="ask" width={400} onTab={() => {}} onClose={() => {}} onResize={() => {}}>
      <Placeholder scope="Run" text="Chat about this run is not available yet." />
    </Pane>,
  );
  assert.equal(html.match(/role="tab"/g)?.length, 5);
  assert.match(html, /aria-selected="true"[^>]*>Ask</);
  assert.match(html, /aria-label="Close pane"/);
  assert.match(html, /<hr tabindex="0" aria-label="Resize the side pane"[^>]*aria-valuenow="400"/);
  assert.match(html, /<span class="scope">Run<\/span>/);
  assert.match(html, /Chat about this run is not available yet\./);
});
