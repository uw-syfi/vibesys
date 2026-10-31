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
    <div style="display:flex;gap:8px;width:200px">
      <div style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="A long idea title that is cut">
        A long idea title that is cut
      </div>
      <span>1.18×</span>
    </div>
    <div class="vs-scrubber-head" style="display:flex;align-items:center;gap:8px"><span style="height:32px;display:inline-flex;align-items:center">Start</span><span class="vs-chip" style="height:32px;box-sizing:border-box">Replay</span><button class="vs-button" style="height:32px;box-sizing:border-box">Jump to end</button></div>
    <input id="clean-range" type="range" aria-label="Run position" style="margin:8px 0 12px;outline:2px solid blue;outline-offset:2px"/>
    <div style="height:14px">Call 1</div>
    <script>document.getElementById('clean-range').focus();</script>
    <svg width="240" height="80" role="img" aria-label="measured ideas">
      <circle cx="40" cy="40" r="6" fill="rgb(200, 40, 40)"/>
      <circle cx="120" cy="30" r="6" fill="rgb(40, 90, 200)"/>
    </svg>
    <div>
      <span style="display:inline-block;width:10px;height:10px;background:rgb(200, 40, 40)"></span> failed
      <span style="display:inline-block;width:10px;height:10px;background:rgb(40, 90, 200)"></span> passed
    </div>
    <p>The planner explained <code>total_ops_per_sec</code> and <code>0123456789abcdef0123</code>.</p>
    <ul style="width:150px;margin:0;padding-left:16px"><li>Served by <code>engine/server.py</code>, echo and logprobs, then more words after them</li></ul>
    <div style="width:80px">we will pad and re-measure the queue</div>
    <div><span>Claude Code · Opus 5.5 · Judge</span> <span>Perf evaluator</span> <span>Planner</span></div>
    <div>Lock-free ring with per-slot sequence numbers</div>
    <div><span data-mono-id>claude-opus-5-5</span> <span data-mono-id>perf-evaluator</span> <code>judge</code></div>
    <div>queue-rs · mpmc</div>
    <div style="display:flex;align-items:center;gap:8px"><span class="vs-searchbox" style="display:inline-flex;height:32px;box-sizing:border-box"><input type="search" aria-label="Search" style="height:30px;box-sizing:border-box"/></span><select aria-label="Sort" style="height:32px;box-sizing:border-box"><option>Newest</option></select></div>
    <div class="vs-pickrow"><span class="vs-pickrow-text"><span>Ring buffer</span><span>Within noise</span></span><span class="vs-chip">Passed</span></div>
    <div style="display:flex;flex-direction:column;height:80px">
      <div style="max-height:60px;overflow-y:auto"><div style="height:200px">rows</div></div>
      <div>Start</div>
    </div>
    <div class="vs-page-content" style="position:relative;width:440px;height:60px">
      <div class="vs-page-scroll" style="width:300px"><div class="vs-page-body"><div>Machine</div></div></div>
      <div class="vs-page-footer" style="position:absolute;left:320px;top:0;width:100px"><div>Ready</div></div>
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
    name: 'a row whose line under its title repeats its badge',
    check: 'repeat',
    message: /a row says "Passed" twice/,
    html: `<div style="${PAGE}"><div class="vs-pickrow"><span class="vs-pickrow-text"><span>Ring buffer</span><span>Passed</span></span><span class="vs-chip">Passed</span></div></div>`,
  },
  {
    name: 'a chart too small to read',
    check: 'chart-size',
    message: /the chart is 240 by 60 px, under the 280 by 100 px/,
    html: `<div style="${PAGE}"><svg class="vs-chart" role="group" aria-label="Metric of each measured idea over the run, with the best so far" width="240" height="60" viewBox="0 0 240 60"><circle cx="40" cy="30" r="4" fill="red"/></svg></div>`,
  },
  {
    name: 'a chart drawn at a third of its size, its labels specks',
    check: 'chart-size',
    message: /the chart label "10m" is drawn 4 px high, under 10 px/,
    html: `<div style="${PAGE}"><svg class="vs-chart" role="group" aria-label="Metric of each measured idea over the run, with the best so far" width="330" height="120" viewBox="0 0 1000 364"><text x="500" y="300" style="font-size:11px">10m</text></svg></div>`,
  },
  {
    name: 'a chart label cut by the drawing’s edge',
    check: 'chart-size',
    message: /the chart label "333h 20m" is cut by the edge of the drawing/,
    html: `<div style="${PAGE}"><svg class="vs-chart" role="group" aria-label="Metric of each measured idea over the run, with the best so far" width="320" height="120" viewBox="0 0 320 120" style="overflow:visible"><text x="300" y="100" style="font-size:12px">333h 20m</text></svg></div>`,
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
    name: 'a search box and a select in one row, the select pushed down by its label',
    check: 'control-row',
    message: /controls in one row do not line up: .* is 32 px high and .* 40 px/,
    html: `<div style="${PAGE}"><div style="display:flex;align-items:center;gap:8px"><span class="vs-searchbox" style="display:inline-flex;height:32px;box-sizing:border-box"><input type="search" aria-label="Search" style="height:30px;box-sizing:border-box"/></span><div style="display:flex;flex-direction:column"><label for="sort" style="height:20px">Sort</label><select id="sort" style="height:40px"><option>Newest</option></select></div></div></div>`,
  },
  {
    name: 'the time bar: position, state chip and action of three heights and tops',
    check: 'control-row',
    message:
      /controls in one row do not line up: .*vs-chip.* is 18 px high and .*vs-button.* 32 px/,
    html: `<div style="${PAGE}"><div class="vs-scrubber-head" style="display:flex;align-items:center;gap:8px"><span>Start</span><span class="vs-chip" style="height:18px;box-sizing:border-box">Replay</span><button class="vs-button" style="height:32px;box-sizing:border-box">Jump to end</button></div></div>`,
  },
  {
    name: 'a name cut with an ellipsis while the rest of its line is empty',
    check: 'truncation-room',
    message:
      /cut with an ellipsis while \d+ px beside it are empty: "Lock-free ring with per-slot sequence nu"/,
    html: `<div style="${PAGE}"><div class="vs-pane"><div style="display:flex;gap:8px"><span>Best so far:</span><div style="width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="Lock-free ring with per-slot sequence numbers">Lock-free ring with per-slot sequence numbers</div></div></div></div>`,
  },
  {
    name: 'a focus ring drawn over the label under the focused range',
    check: 'focus-ring',
    message: /the focus ring of input .* is drawn over the text "Call 1"/,
    html: `<div style="${PAGE}"><input id="range" type="range" aria-label="Run position" style="display:block;margin:0;outline:2px solid blue;outline-offset:2px"/><div>Call 1</div><script>document.getElementById('range').focus();</script></div>`,
  },
  {
    name: 'a side card that starts level with the page title while the form starts below it',
    check: 'column-tops',
    message: /the side column starts \d+ px from the top of the main column/,
    html: `<div style="${PAGE}"><div class="vs-page-content" style="position:relative;width:440px;height:200px"><div class="vs-page-scroll" style="width:300px"><div class="vs-page-body" style="padding-top:60px"><div>Machine</div></div></div><div class="vs-page-footer" style="position:absolute;left:320px;top:0;width:100px"><div>Ready</div></div></div></div>`,
  },
  {
    name: 'a full-width strip tinted by a warn fill under a new class name, with its text and a button',
    check: 'banner',
    message:
      /a strip 100% of the main pane wide is tinted by a status color: "Login started Check again"/,
    html: `<style>:root{--chip-warn-bg:#f3e6c8;--chip-err-bg:#f6dad6}</style><main style="display:block;width:440px"><div class="vs-notice" style="display:flex;gap:8px;background:var(--chip-warn-bg)">Login started <button>Check again</button></div></main>`,
  },
  {
    name: 'text drawn outside the window',
    check: 'overflow',
    message: /text runs outside the window/,
    html: `<div style="${PAGE}"><span style="position:absolute;left:-20px;top:20px">gone left</span></div>`,
  },
  {
    name: 'a back button drawn in the corner the macOS traffic lights sit in',
    check: 'chrome-clear',
    message: /button "Back" is drawn where the window's own controls are/,
    html: `<style>:root{--chrome-start-width:80px;--chrome-start-height:40px}</style><div style="${PAGE}"><button>Back</button></div>`,
  },
  {
    name: 'a title drawn in the strip the Windows controls are drawn over',
    check: 'chrome-clear',
    message: /span "Run title" is drawn where the window's own controls are/,
    html: `<style>:root{--chrome-end-height:40px}</style><div class="vs-app-main" style="${PAGE}"><span>Run title</span></div>`,
  },
  {
    name: 'a link left inside the window drag region',
    check: 'drag-region',
    message: /a "Open" is in the window's drag region/,
    html: `<div style="-webkit-app-region:drag;height:60px;${PAGE}"><a href="#open">Open</a></div>`,
  },
];

/**
 * A page that lays out around the window's own controls the way the app does, which the chrome
 * checks pass: a start column that drags, a top band that drags, and the page's controls carved out.
 */
export const CLEAN_CHROME_SCREEN = `
  <style>:root{--chrome-start-width:80px;--chrome-start-height:40px}</style>
  <div style="${PAGE}display:flex;padding:0">
    <nav style="-webkit-app-region:drag;width:80px;padding-top:48px;box-sizing:border-box;height:320px">
      <button style="-webkit-app-region:no-drag">New</button>
    </nav>
    <div class="vs-app-main" style="position:relative;isolation:isolate;flex:1">
      <div style="position:absolute;top:0;left:0;right:0;height:16px;z-index:-1;-webkit-app-region:drag"></div>
      <div style="padding:24px"><h1 style="font-size:20px;margin:0">Run</h1><button style="-webkit-app-region:no-drag">Chat</button></div>
    </div>
  </div>`;
