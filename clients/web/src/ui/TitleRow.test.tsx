import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {Retained, RunStatus, TitleRow} from './TitleRow.js';

test('the title row: run name with its objective as the hint, status, labelled metric', () => {
  const html = renderToStaticMarkup(
    <TitleRow
      title="Increase decode throughput"
      objective="The whole objective."
      project="llm-serve"
    >
      <RunStatus
        line={{text: 'Judging round 6', busy: false, paused: false, activeKind: 'judge'}}
      />
      <Retained
        text={{
          label: 'Retained',
          value: '1,230',
          unit: 'tok/s',
          change: {text: '+29%', tone: 'ok'},
          hint: 'Kept checkpoint of this run (round 5). Baseline 950 tok/s.',
        }}
      />
    </TitleRow>,
  );
  assert.match(
    html,
    /<span class="name" title="The whole objective.">Increase decode throughput<\/span>/,
  );
  assert.match(html, /<span class="proj">llm-serve<\/span>/);
  assert.match(html, /aria-live="polite">Judging round 6</);
  assert.match(html, /Retained <b>1,230<\/b>/);
  assert.match(html, /<span class="ok">\+29%<\/span><span class="lbl"> vs baseline<\/span>/);
});

test('a status in transition shows a spinner', () => {
  const html = renderToStaticMarkup(
    <RunStatus
      line={{text: 'Pausing after the current call…', busy: true, paused: false, activeKind: null}}
    />,
  );
  assert.match(html, /class="spin"/);
});
