import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {StartFailure} from './StartFailure.js';

const none = () => undefined;

test('a failed start shows its message, a copyable stderr tail, the log path, Retry and Back', () => {
  const html = renderToStaticMarkup(
    <StartFailure
      failure={{
        message: 'The run server exited before the run started.',
        tail: [
          'error[E0599]: no method named `decode_step` found',
          '  --> benches/decode.rs:41:14',
        ],
        log: '/Users/me/.vibesys/web/gateways/p/live.stderr',
      }}
      root="/Users/me/src/llm-serve"
      backLabel="Back to setup"
      onRetry={none}
      onBack={none}
    />,
  );
  assert.match(
    html,
    /<p class="claim">The run server exited before the run started\. Your settings are kept\.<\/p>/,
  );
  assert.match(
    html,
    /<div class="out"><pre>error\[E0599\]: no method named `decode_step` found\n {2}--&gt; <button type="button" class="linkish" title="Copy \/Users\/me\/src\/llm-serve\/benches\/decode.rs:41:14">benches\/decode.rs:41:14<\/button>\n<\/pre><\/div>/,
  );
  assert.match(
    html,
    /<p class="logline">Full log <button type="button" class="linkish mono" title="Copy the path">\/Users\/me\/.vibesys\/web\/gateways\/p\/live.stderr<\/button><\/p>/,
  );
  assert.match(
    html,
    /<button type="button" class="btn primary">Retry<\/button><button type="button" class="btn ghost">Back to setup<\/button>/,
  );
});

test('a failure without stderr shows no empty block', () => {
  const html = renderToStaticMarkup(
    <StartFailure
      failure={{message: 'The run server stopped answering.', tail: [], log: null}}
      root={null}
      backLabel="Back"
      onRetry={none}
      onBack={none}
    />,
  );
  assert.doesNotMatch(html, /class="out"|logline/);
});
