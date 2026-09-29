import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {RunControl} from '../model.js';
import type {Menu} from '../ui-state.js';
import {
  HomeMenu,
  MoreMenu,
  Retained,
  RunControlChip,
  RunStatus,
  ThemeItems,
  TitleRow,
} from './TitleRow.js';

const pause: RunControl = {
  kind: 'action',
  action: 'pause',
  label: 'Pause',
  tip: 'Pause after the current agent call',
  disabled: false,
};

test('the title row: run name with its objective as the hint, status, labelled metric', () => {
  const html = renderToStaticMarkup(
    <TitleRow
      title="Increase decode throughput"
      objective="The whole objective."
      project="llm-serve"
    >
      <RunStatus
        line={{text: 'Judging round 6', busy: false, paused: false, activeKind: 'judge'}}
        control={pause}
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
      control={pause}
    />,
  );
  assert.match(html, /class="spin"/);
});

test('the run control: Pause while running; nothing while pending or after the end', () => {
  const html = renderToStaticMarkup(
    <RunControlChip control={pause} busy={false} onToggle={() => {}} />,
  );
  assert.match(html, /title="Pause after the current agent call"[^>]*>.*Pause<\/button>/s);
  assert.equal(
    renderToStaticMarkup(<RunControlChip control={pause} busy onToggle={() => {}} />),
    '',
  );
  const ended: RunControl = {kind: 'ended', word: 'Completed', summary: null, tip: null};
  assert.equal(
    renderToStaticMarkup(<RunControlChip control={ended} busy={false} onToggle={() => {}} />),
    '',
  );
});

test('stop asks first in a popover under ••• and names who finishes; none once the run cannot stop', () => {
  const menu = (canStop: boolean, open: Menu) =>
    renderToStaticMarkup(
      <MoreMenu
        menu={open}
        canStop={canStop}
        stopWho="judge"
        runId="run-1"
        resume={null}
        theme="system"
        onMenu={() => {}}
        onStop={() => {}}
        onNotes={() => {}}
        onTheme={() => {}}
      />,
    );
  assert.match(
    menu(true, 'stop'),
    /<div class="pop" role="alertdialog" aria-labelledby="stop-title"><div class="confirm">/,
  );
  assert.match(menu(true, 'stop'), /The judge finishes its call, then no further rounds start\./);
  assert.equal(menu(false, 'stop').includes('alertdialog'), false);
  // The run ended with the confirmation open: ••• is no longer pressed.
  assert.match(menu(false, 'stop'), /class="iconbtn"[^>]*aria-expanded="false"/);
  assert.match(menu(false, 'more'), /title="Notes, copy run ID, theme"/);
  assert.match(menu(true, 'more'), /title="Notes, copy run ID, theme, stop the run"/);
  // Run items, then Theme, then Stop.
  assert.match(
    menu(true, 'more'),
    />Notes<.*>Copy run ID<\/button><div class="sepl"><\/div><fieldset class="grp">.*>Dark<.*<div class="sepl"><\/div>.*>Stop run…</,
  );
  assert.match(menu(true, 'more'), /role="menuitem"[^>]*>Stop run…</);
  assert.equal(menu(false, 'more').includes('Stop run'), false);
  assert.match(menu(false, 'more'), />Copy run ID</);
});

test('a failed run says why: the summary in the row, the full diagnostic as its hint', () => {
  const failed: RunControl = {
    kind: 'ended',
    word: 'Failed',
    summary: 'Benchmark harness crashed',
    tip: 'Benchmark harness crashed: exit 137 after 41.7s (OOM killer)',
  };
  const html = renderToStaticMarkup(
    <RunStatus
      line={{text: 'Failed', busy: false, paused: false, activeKind: null}}
      control={failed}
    />,
  );
  assert.match(html, /title="Benchmark harness crashed: exit 137 after 41.7s \(OOM killer\)"/);
  assert.match(html, /<span class="why">Failed: Benchmark harness crashed<\/span>/);
  const completed = renderToStaticMarkup(
    <RunStatus
      line={{text: 'Completed', busy: false, paused: false, activeKind: null}}
      control={{kind: 'ended', word: 'Completed', summary: null, tip: null}}
    />,
  );
  assert.equal(completed.includes('title='), false);
});

test('the theme items: one radio per choice, the current one checked', () => {
  const html = renderToStaticMarkup(<ThemeItems theme="light" onTheme={() => {}} />);
  assert.match(html, /<fieldset class="grp"><legend class="gh">Theme<\/legend>/);
  assert.equal(html.match(/role="menuitemradio"/g)?.length, 3);
  assert.match(html, /aria-checked="true" class="it">Light/);
});

test('an ended run the home can resume: Resume run… leads the menu and the hint', () => {
  const html = renderToStaticMarkup(
    <MoreMenu
      menu="more"
      canStop={false}
      stopWho="judge"
      runId="run-1"
      resume="/resume"
      theme="dark"
      onMenu={() => {}}
      onStop={() => {}}
      onNotes={() => {}}
      onTheme={() => {}}
    />,
  );
  assert.match(html, /title="Resume, notes, copy run ID, theme"/);
  assert.match(
    html,
    /<div class="pop" role="menu" aria-label="Run"><a role="menuitem" class="it" href="\/resume">Resume run…/,
  );
});

test("the home window's ••• holds the theme and says so", () => {
  const html = renderToStaticMarkup(<HomeMenu theme="light" onTheme={() => {}} />);
  assert.match(html, /title="Theme" aria-label="More" aria-haspopup="menu"/);
});
