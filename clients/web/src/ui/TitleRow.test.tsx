import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {RunControl} from '../model.js';
import type {Menu} from '../ui-state.js';
import {MoreMenu, Retained, RunControlChip, RunStatus, TitleRow} from './TitleRow.js';

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

test('the project label is hidden when it repeats the title (no objective, so title falls back to it)', () => {
  const html = renderToStaticMarkup(
    <TitleRow title="llm-serve" objective={null} project="llm-serve">
      <span />
    </TitleRow>,
  );
  assert.equal(html.includes('class="proj"'), false);
});

test('a status in transition shows a spinner', () => {
  const html = renderToStaticMarkup(
    <RunStatus
      line={{text: 'Pausing after the current call…', busy: true, paused: false, activeKind: null}}
    />,
  );
  assert.match(html, /class="spin"/);
});

const pause: RunControl = {
  kind: 'action',
  action: 'pause',
  label: 'Pause',
  tip: 'Pause after the current agent call',
  disabled: false,
};

test('the run control: Pause while running; nothing while pending or after the end', () => {
  const html = renderToStaticMarkup(
    <RunControlChip control={pause} busy={false} onToggle={() => {}} />,
  );
  assert.match(html, /title="Pause after the current agent call"[^>]*>.*Pause<\/button>/s);
  assert.equal(
    renderToStaticMarkup(<RunControlChip control={pause} busy onToggle={() => {}} />),
    '',
  );
  const ended: RunControl = {kind: 'ended', word: 'Completed', tip: null};
  assert.equal(
    renderToStaticMarkup(<RunControlChip control={ended} busy={false} onToggle={() => {}} />),
    '',
  );
});

test('stop asks first in a modal dialog and names who finishes; none once the run cannot stop', () => {
  const menu = (canStop: boolean, open: Menu) =>
    renderToStaticMarkup(
      <MoreMenu
        menu={open}
        canStop={canStop}
        stopWho="judge"
        runId="run-1"
        onMenu={() => {}}
        onStop={() => {}}
      />,
    );
  assert.match(
    menu(true, 'stop'),
    /<dialog class="confirm" role="alertdialog" aria-labelledby="stop-title">/,
  );
  assert.match(menu(true, 'stop'), /The judge finishes its call, then no further rounds start\./);
  assert.equal(menu(false, 'stop').includes('<dialog'), false);
  assert.match(menu(true, 'more'), /role="menuitem"[^>]*>Stop run…</);
  assert.equal(menu(false, 'more').includes('Stop run'), false);
  assert.match(menu(false, 'more'), />Copy run ID</);
});
