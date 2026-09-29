import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {Evidence} from '../experiments.js';
import {Experiments, type ExperimentsProps} from './Experiments.js';

const EVIDENCE: Evidence[] = [
  {
    round: 3,
    title: 'Skip the post-sampling device sync',
    outcome: {text: 'Failed', tone: 'bad'},
    value: null,
    facts: [{term: 'Pass criteria', text: 'Tests pass.', mono: false}],
  },
];
const props: ExperimentsProps = {
  chart: null,
  evidence: EVIDENCE,
  design: [{round: 3, files: 'src/sampler.rs', summary: 'Removed the sync.', reverted: true}],
  view: 'hypotheses',
  open: null,
  progress: '6 of 12 rounds',
  onView: () => {},
  onToggle: () => {},
  onRound: () => {},
  onChanges: () => {},
};

test('hypotheses: rows collapsed until opened; an open row shows its evidence and links', () => {
  const closed = renderToStaticMarkup(<Experiments {...props} />);
  assert.match(closed, /<span class="scope">Run<\/span><span>6 of 12 rounds<\/span>/);
  assert.match(closed, /aria-expanded="false"[^>]*><span class="id">r3<\/span>/);
  assert.match(closed, /<span class="oc bad">Failed<\/span>/);
  assert.match(closed, /No round has a measurement yet\./);
  const open = renderToStaticMarkup(<Experiments {...props} open={3} />);
  assert.match(open, /<dt>Pass criteria<\/dt><dd>Tests pass\.<\/dd>/);
  assert.match(open, />Open round 3</);
  assert.match(open, />View changes</);
});

test('design: one row per round with its files; reverted rounds say so', () => {
  const html = renderToStaticMarkup(<Experiments {...props} view="design" />);
  assert.match(html, /aria-pressed="true"[^>]*>Design</);
  assert.match(html, /<div class="mono">src\/sampler.rs<\/div>/);
  assert.match(html, />reverted</);
});
