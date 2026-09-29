import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {ChartModel, Evidence} from '../experiments.js';
import {Experiments, type ExperimentsProps} from './Experiments.js';

const EVIDENCE: Evidence[] = [
  {
    round: 3,
    title: 'Skip the post-sampling device sync',
    outcome: {text: 'Failed', tone: 'bad'},
    value: '1,234',
    valueLabel: '1,234 tok/s',
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
  assert.match(closed, /<span class="m" title="1,234 tok\/s">1,234<\/span>/);
  const open = renderToStaticMarkup(<Experiments {...props} open={3} />);
  assert.match(open, /<dt>Pass criteria<\/dt><dd>Tests pass\.<\/dd>/);
  assert.match(open, />Open round 3</);
  assert.match(open, />View changes</);
});

test('chart: the axis labels carry the unit as a hover hint', () => {
  const chart: ChartModel = {
    width: 368,
    height: 136,
    floor: 110.5,
    path: '',
    points: [],
    planned: [],
    ticks: [],
    baseline: {value: '950', y: 90, label: 'Baseline: 950 tok/s'},
    retained: {value: '1,230', y: 20, label: 'Retained: 1,230 tok/s'},
  };
  const html = renderToStaticMarkup(<Experiments {...props} chart={chart} />);
  assert.match(html, /<text[^>]*><title>Baseline: 950 tok\/s<\/title>950<\/text>/);
  assert.match(html, /<text class="v"[^>]*><title>Retained: 1,230 tok\/s<\/title>1,230<\/text>/);
});

test('design: one row per round with its files; reverted rounds say so', () => {
  const html = renderToStaticMarkup(<Experiments {...props} view="design" />);
  assert.match(html, /aria-pressed="true"[^>]*>Design</);
  assert.match(html, /<div class="mono">src\/sampler.rs<\/div>/);
  assert.match(html, />reverted</);
});
