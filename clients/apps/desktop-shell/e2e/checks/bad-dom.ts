/**
 * Screens known to be wrong, as small pages: one for each defect users reported and each check the
 * screen checks make. `screen-checks.spec.ts` loads each in the browser and requires that the check
 * named here fails on it, and that a clean page passes every check, so a check that stops looking
 * (or starts crying wolf) fails its own test. Fonts are the browser's defaults and every size is
 * fixed, so the layout is the same on every machine.
 */
import type {CheckId} from './in-page.js';

export interface BadScreen {
  readonly name: string;
  /** The check that must fail on `html`. */
  readonly check: CheckId;
  /** What its message says. */
  readonly message: RegExp;
  readonly html: string;
}

const PAGE = 'font: 16px/20px monospace; margin: 0; padding: 8px;';

/** A page that every check passes: labelled numbers, a legend, tooltips, nothing overlapping. */
export const CLEAN_SCREEN = `
  <div style="${PAGE}">
    <h1 style="font-size:20px">Run</h1>
    <div><span>best path</span> <span>Lock-free ring</span> <span>1.18×</span></div>
    <div>ops/s <span>48,000,000</span></div>
    <div style="width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="A long idea title that is cut">
      A long idea title that is cut
    </div>
    <svg width="240" height="80" role="img" aria-label="measured ideas">
      <circle cx="40" cy="40" r="6" fill="rgb(200, 40, 40)"/>
      <circle cx="120" cy="30" r="6" fill="rgb(40, 90, 200)"/>
    </svg>
    <div>
      <span style="display:inline-block;width:10px;height:10px;background:rgb(200, 40, 40)"></span> failed
      <span style="display:inline-block;width:10px;height:10px;background:rgb(40, 90, 200)"></span> passed
    </div>
    <p>The planner explained <code>total_ops_per_sec</code> and <code>0123456789abcdef0123</code>.</p>
    <div style="width:80px">we will pad and re-measure the queue</div>
    <div><span>Claude Code · Opus 5.5 · Judge</span> <span>Perf evaluator</span> <span>Planner</span></div>
    <div>Lock-free ring with per-slot sequence numbers</div>
    <div><span data-mono-id>claude-opus-5-5</span> <span data-mono-id>perf-evaluator</span> <code>judge</code></div>
    <div>queue-rs · mpmc</div>
    <div style="display:flex;flex-direction:column;height:80px">
      <div style="max-height:60px;overflow-y:auto"><div style="height:200px">rows</div></div>
      <div>Start</div>
    </div>
    <div class="vs-pane-body" style="width:440px">
      <div style="max-width:100ch">The judge read the candidate source and found that it matches the declared outcome, so the idea passed.</div>
    </div>
  </div>`;

export const BAD_SCREENS: readonly BadScreen[] = [
  {
    name: 'a ratio wrapping one character per line',
    check: 'wrap',
    message: /wraps onto \d+ lines/,
    html: `<div style="${PAGE}"><div style="width:12px;overflow-wrap:anywhere"><span>1.18×</span></div></div>`,
  },
  {
    name: 'a label broken in the middle of its word',
    check: 'wrap',
    message: /a word is broken across lines/,
    html: `<div style="${PAGE}"><div style="width:20px;overflow-wrap:anywhere">best path</div></div>`,
  },
  {
    name: 'a header whose texts overlap',
    check: 'overlap',
    message: /text overlaps text: "Combine W1 ring with W2 paddin" and "Running"/,
    html: `<div style="${PAGE}position:relative;height:60px">
      <h1 style="font-size:20px;margin:0">Combine W1 ring with W2 padding</h1>
      <span style="position:absolute;left:60px;top:6px">Running</span>
    </div>`,
  },
  {
    name: 'a raw failure reason in a chip',
    check: 'raw-text',
    message: /enum value "implementation-failed"|snake_case identifier "implementation_failed"/,
    html: `<div style="${PAGE}"><span class="chip">implementation_failed</span></div>`,
  },
  {
    name: 'a raw kebab-case reason',
    check: 'raw-text',
    message: /enum value "implementation-failed"/,
    html: `<div style="${PAGE}"><span>implementation-failed</span></div>`,
  },
  {
    name: 'a model id with its vendor prefix dropped, as a label',
    check: 'raw-text',
    message: /code name "opus-5-5"/,
    html: `<div style="${PAGE}"><span class="chip">opus-5-5</span></div>`,
  },
  {
    name: 'a model id in a chip',
    check: 'raw-text',
    message: /registry id "claude-opus-5-5"/,
    html: `<div style="${PAGE}"><span class="chip">claude-opus-5-5</span></div>`,
  },
  {
    name: 'a role id as a title',
    check: 'raw-text',
    message: /registry id "judge"/,
    html: `<div style="${PAGE}"><h2>judge</h2></div>`,
  },
  {
    name: 'a role id after a separator and in a short phrase',
    check: 'raw-text',
    message: /registry id "perf-evaluator"/,
    html: `<div style="${PAGE}"><span>agent turn · perf-evaluator</span> <span>waiting after judge</span></div>`,
  },
  {
    name: 'a harness id as a label',
    check: 'raw-text',
    message: /registry id "claude"/,
    html: `<div style="${PAGE}"><span>claude</span></div>`,
  },
  {
    name: 'a role name in lowercase',
    check: 'raw-text',
    message: /lowercase name "perf evaluator"/,
    html: `<div style="${PAGE}"><span>Run · perf evaluator</span></div>`,
  },
  {
    name: 'a template hole, undefined, NaN, null and a hash in the text',
    check: 'raw-text',
    message: /template hole/,
    html: `<div style="${PAGE}"><p>Idea \${title}</p><p>undefined</p><p>NaN×</p><p>null</p><p>0123456789abcdef0123</p></div>`,
  },
  {
    name: 'colored marks with no legend',
    check: 'legend',
    message: /no legend item names it/,
    html: `<div style="${PAGE}"><svg width="240" height="80">
      <circle cx="40" cy="40" r="6" fill="rgb(200, 40, 40)"/>
      <circle cx="120" cy="30" r="6" fill="rgb(40, 90, 200)"/>
    </svg></div>`,
  },
  {
    name: 'a color strip with no legend',
    check: 'legend',
    message: /no legend item names it/,
    html: `<div style="${PAGE}"><div style="display:flex;width:200px;height:8px">
      <div style="flex:1;background:rgb(200, 40, 40)"></div>
      <div style="flex:1;background:rgb(40, 160, 90)"></div>
      <div style="flex:1;background:rgb(40, 90, 200)"></div>
    </div></div>`,
  },
  {
    name: 'a number with no label or unit',
    check: 'number-label',
    message: /the number 24,000,000 has no label or unit/,
    html: `<div style="${PAGE}"><div><span>24,000,000</span></div></div>`,
  },
  {
    name: 'text cut with an ellipsis and no tooltip',
    check: 'truncation',
    message: /cut with an ellipsis and has no tooltip/,
    html: `<div style="${PAGE}"><div style="width:80px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">A long idea title that is cut</div></div>`,
  },
  {
    name: 'a page wider than the window',
    check: 'overflow',
    message: /the page is \d+ px wide/,
    html: `<div style="${PAGE}"><div style="width:3000px;height:10px;background:#888"></div></div>`,
  },
  {
    name: 'a pane that scrolls sideways',
    check: 'overflow',
    message: /a pane scrolls sideways/,
    html: `<div style="${PAGE}"><div style="width:200px;overflow-x:auto"><div style="width:600px">wide content</div></div></div>`,
  },
  {
    name: 'a count struck through',
    check: 'strike',
    message: /text is struck through and reads as deleted: "2 failed"/,
    html: `<div style="${PAGE}"><span style="text-decoration:line-through">2 failed</span></div>`,
  },
  {
    name: 'a struck word inside a chip',
    check: 'strike',
    message: /struck through and reads as deleted: "failed"/,
    html: `<div style="${PAGE}"><button><s><span>failed</span></s></button></div>`,
  },
  {
    name: 'a performance chart a third of the readable width',
    check: 'chart-size',
    message: /the performance chart is 330 by 60 px/,
    html: `<div style="${PAGE}"><svg role="group" aria-label="Metric of each measured idea over the run, with the best so far" width="330" height="60"><circle cx="40" cy="30" r="4" fill="red"/></svg></div>`,
  },
  {
    name: 'an agent turn in a column a third of its pane, with dead space beside it',
    check: 'pane-fill',
    message: /a pane's content uses 160 px of its 440 px \(36%\) while text wraps/,
    html: `<div style="${PAGE}"><div class="vs-pane-body" style="width:440px"><div style="max-width:160px">The candidate source matches the declared outcome and the gates passed.</div></div></div>`,
  },
  {
    name: 'a card whose list scrolls although the card has room below it',
    check: 'scroll-room',
    message: /a region scrolls \(\d+ px of content in 60 px\) while it has \d+ px of room/,
    html: `<div style="${PAGE}"><div style="display:flex;flex-direction:column;height:240px"><div style="max-height:60px;overflow-y:auto"><div style="height:200px">rows</div></div><div style="margin-top:auto">Start</div></div></div>`,
  },
  {
    name: 'text drawn outside the window',
    check: 'overflow',
    message: /text runs outside the window/,
    html: `<div style="${PAGE}"><span style="position:absolute;left:-20px;top:20px">gone left</span></div>`,
  },
];
