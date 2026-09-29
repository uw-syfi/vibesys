import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {PaletteItem} from '../palette.js';
import {Palette} from './Palette.js';

const ITEMS: PaletteItem[] = [
  {
    id: 'run-toggle',
    group: 'Run',
    label: 'Pause after the current agent call',
    detail: '',
    keys: '',
    intent: {kind: 'toggleRun'},
  },
  {
    id: 'round-1',
    group: 'Go to',
    label: 'Round 1',
    detail: 'Batch decode steps',
    keys: '',
    intent: {kind: 'ui', action: {type: 'round', round: 1, live: 2}},
  },
];

test('the palette: a modal dialog with a combobox over grouped options, the first one active, a result count', () => {
  const html = renderToStaticMarkup(<Palette items={ITEMS} onRun={() => {}} onClose={() => {}} />);
  assert.match(html, /<dialog class="pal" aria-label="Search and commands">/);
  assert.match(html, /role="combobox"[^>]*aria-activedescendant="pal-run-toggle"/);
  assert.match(html, /<div class="gh">Run<\/div>/);
  assert.match(html, /id="pal-run-toggle"[^>]*aria-selected="true"/);
  assert.match(html, /<span class="d">Batch decode steps<\/span>/);
  assert.match(html, />2 results</);
});
