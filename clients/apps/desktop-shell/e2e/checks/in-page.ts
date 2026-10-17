/**
 * The layout and wording checks, as they run inside the page against its real layout. Every
 * function here is sent to the page by source (`screen-check.ts` joins them into one script), so
 * each is self-contained: it uses only the DOM, its own arguments and the other functions in
 * `HELPERS`, and no import. A check reads the screen and returns what is wrong with it; it never
 * changes the page.
 */

export type CheckId =
  | 'overlap'
  | 'wrap'
  | 'overflow'
  | 'truncation'
  | 'raw-text'
  | 'legend'
  | 'number-label'
  | 'strike'
  | 'chart-size'
  | 'pane-fill'
  | 'stability';

export interface Violation {
  readonly check: CheckId;
  /** What is wrong, in a sentence naming the offending text. */
  readonly message: string;
  /** Where: the element's tag, classes and text. */
  readonly where: string;
}

/** The words a check reads from the page: the run's closed sets that must never show raw. */
export interface CheckInput {
  /** Enum values that never read as words (`implementation-failed`). */
  readonly rawWords: readonly string[];
}

interface Box {
  readonly left: number;
  readonly top: number;
  readonly right: number;
  readonly bottom: number;
}

interface TextBox {
  readonly node: Text;
  readonly element: Element;
  readonly text: string;
  /** The line boxes of the text that are on screen. */
  readonly lines: readonly Box[];
  /** Whether a line runs past the window's edge without any container clipping it there. */
  readonly spills: boolean;
}

/** Whether `element` is code a person reads as code: identifiers are allowed there. */
function inCode(element: Element): boolean {
  return element.closest('code, pre, kbd, samp, script, style, textarea, [data-mono-id]') !== null;
}

function describe(element: Element): string {
  const classes = [...element.classList].slice(0, 2).join('.');
  const text = (element.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 40);
  return `${element.tagName.toLowerCase()}${classes === '' ? '' : `.${classes}`} "${text}"`;
}

function boxOf(rect: DOMRect): Box {
  return {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom};
}

function cut(a: Box, b: Box): Box | null {
  const box = {
    left: Math.max(a.left, b.left),
    top: Math.max(a.top, b.top),
    right: Math.min(a.right, b.right),
    bottom: Math.min(a.bottom, b.bottom),
  };
  return box.right > box.left && box.bottom > box.top ? box : null;
}

/** What the clipping elements around `element` (itself included) let through; unbounded if none. */
function clipArea(element: Element): Box | null {
  const far = 1e9;
  let area: Box | null = {left: -far, top: -far, right: far, bottom: far};
  for (let up: Element | null = element; up !== null && area !== null; up = up.parentElement) {
    const style = getComputedStyle(up);
    if (style.overflowX !== 'visible' || style.overflowY !== 'visible') {
      area = cut(area, boxOf(up.getBoundingClientRect()));
    }
  }
  return area;
}

function isShown(element: Element): boolean {
  for (let up: Element | null = element; up !== null; up = up.parentElement) {
    const style = getComputedStyle(up);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
      return false;
    }
  }
  return true;
}

/** The text on screen: each non-blank text node of a shown element, with its visible line boxes. */
function textBoxes(): TextBox[] {
  const found: TextBox[] = [];
  const viewport: Box = {left: 0, top: 0, right: innerWidth, bottom: innerHeight};
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    const text = (node.textContent ?? '').replace(/\s+/g, ' ').trim();
    const element = node.parentElement;
    if (text === '' || element === null || !isShown(element)) continue;
    const clip = clipArea(element);
    const range = document.createRange();
    range.selectNodeContents(node);
    const own = [...range.getClientRects()]
      .map(rect => (clip === null ? null : cut(boxOf(rect), clip)))
      .filter((box): box is Box => box !== null && box.right - box.left > 1);
    const lines = own.map(box => cut(box, viewport)).filter((box): box is Box => box !== null);
    const spills = own.some(box => box.right > innerWidth + 1 || box.left < -1);
    if (lines.length > 0) found.push({node: node as Text, element, text, lines, spills});
  }
  return found;
}

/** The distinct line tops of a text node's own rectangles (its lines, wrapped or not). */
function lineCount(node: Text): number {
  const range = document.createRange();
  range.selectNodeContents(node);
  const tops: number[] = [];
  for (const rect of range.getClientRects()) {
    if (rect.width > 0 && !tops.some(top => Math.abs(top - rect.top) < rect.height / 2)) {
      tops.push(rect.top);
    }
  }
  return tops.length;
}

/**
 * The words of at most `limit` characters that a text node breaks across two lines. A hyphen is a
 * place a line may break (`re-` then `measure`), so a word counts as broken only inside one of its
 * hyphen-separated parts.
 */
function brokenWords(node: Text, limit: number): string[] {
  const text = node.textContent ?? '';
  const broken: string[] = [];
  for (const match of text.matchAll(/\S+/g)) {
    if (match[0].length < 2 || match[0].length > limit) continue;
    let from = match.index ?? 0;
    for (const part of match[0].split(/(?<=-)/)) {
      const start = from;
      from += part.length;
      if (part.length < 2) continue;
      const first = document.createRange();
      first.setStart(node, start);
      first.setEnd(node, start + 1);
      const last = document.createRange();
      last.setStart(node, start + part.length - 1);
      last.setEnd(node, start + part.length);
      const a = first.getBoundingClientRect();
      const b = last.getBoundingClientRect();
      if (a.height > 0 && Math.abs(a.top - b.top) > a.height / 2) {
        broken.push(match[0]);
        break;
      }
    }
  }
  return broken;
}

function toRgb(color: string): [number, number, number, number] {
  const canvas = document.createElement('canvas');
  canvas.width = 1;
  canvas.height = 1;
  const context = canvas.getContext('2d', {willReadFrequently: true});
  if (context === null) return [0, 0, 0, 0];
  context.clearRect(0, 0, 1, 1);
  context.fillStyle = color;
  context.fillRect(0, 0, 1, 1);
  const [r = 0, g = 0, b = 0, a = 0] = context.getImageData(0, 0, 1, 1).data;
  return [r, g, b, a];
}

function sameColor(a: string, b: string): boolean {
  const [r1, g1, b1] = toRgb(a);
  const [r2, g2, b2] = toRgb(b);
  return Math.hypot(r1 - r2, g1 - g2, b1 - b2) <= 24;
}

/** Whether two line boxes share most of a line's height and a few pixels of width. */
function drawnOver(a: Box, b: Box): boolean {
  const shared = cut(a, b);
  if (shared === null) return false;
  const height = Math.min(a.bottom - a.top, b.bottom - b.top);
  return shared.bottom - shared.top >= height * 0.4 && shared.right - shared.left >= 2;
}

/** Two text boxes that draw over each other: a vertical overlap of most of a line, and a width. */
export function overlapCheck(): Violation[] {
  const lines = textBoxes().flatMap(box => box.lines.map(line => ({box, line})));
  lines.sort((a, b) => a.line.left - b.line.left);
  const found: Violation[] = [];
  for (const [index, a] of lines.entries()) {
    for (const b of lines.slice(index + 1)) {
      if (b.line.left >= a.line.right) break;
      if (a.box.node === b.box.node || !drawnOver(a.line, b.line)) continue;
      found.push({
        check: 'overlap',
        message: `text overlaps text: "${a.box.text.slice(0, 30)}" and "${b.box.text.slice(0, 30)}"`,
        where: `${describe(a.box.element)} / ${describe(b.box.element)}`,
      });
    }
  }
  return found;
}

/** A short text on several lines (a chip, number or badge), or a short word broken mid-word. */
export function wrapCheck(): Violation[] {
  const found: Violation[] = [];
  for (const {node, element, text} of textBoxes()) {
    if (inCode(element)) continue;
    const lines = lineCount(node);
    if (lines > 1 && text.length <= 24) {
      found.push({
        check: 'wrap',
        message: `"${text}" is ${text.length} characters and wraps onto ${lines} lines`,
        where: describe(element),
      });
    }
    const broken = lines > 1 ? brokenWords(node, 16) : [];
    if (broken.length > 0) {
      found.push({
        check: 'wrap',
        message: `a word is broken across lines: ${broken.slice(0, 3).join(', ')}`,
        where: describe(element),
      });
    }
  }
  return found;
}

/** The page or a pane scrolling or clipping sideways, or text drawn outside the window. */
export function overflowCheck(): Violation[] {
  const found: Violation[] = [];
  const root = document.documentElement;
  if (root.scrollWidth > innerWidth + 1) {
    found.push({
      check: 'overflow',
      message: `the page is ${root.scrollWidth} px wide in a ${innerWidth} px window`,
      where: 'html',
    });
  }
  for (const element of document.body.querySelectorAll('*')) {
    const style = getComputedStyle(element);
    const sideways = style.overflowX === 'auto' || style.overflowX === 'scroll';
    if (sideways && element.scrollWidth > element.clientWidth + 1 && isShown(element)) {
      found.push({
        check: 'overflow',
        message: `a pane scrolls sideways: ${element.scrollWidth} px of content in ${element.clientWidth} px`,
        where: describe(element),
      });
    }
  }
  for (const {element, text, spills} of textBoxes()) {
    if (spills) {
      found.push({
        check: 'overflow',
        message: `text runs outside the window: "${text.slice(0, 40)}"`,
        where: describe(element),
      });
    }
  }
  return found;
}

/** Text cut short with an ellipsis that offers no tooltip to read the rest. */
export function truncationCheck(): Violation[] {
  const found: Violation[] = [];
  for (const element of document.body.querySelectorAll('*')) {
    const style = getComputedStyle(element);
    const clamped = style.webkitLineClamp !== 'none' && style.webkitLineClamp !== '';
    const ellipsis = style.textOverflow === 'ellipsis';
    if (!(clamped || ellipsis) || !isShown(element)) continue;
    const cutShort =
      element.scrollWidth > element.clientWidth + 1 ||
      (clamped && element.scrollHeight > element.clientHeight + 1);
    const explained = element.closest('[title], [data-tooltip]') !== null;
    if (cutShort && !explained) {
      found.push({
        check: 'truncation',
        message: `text is cut with an ellipsis and has no tooltip: "${(element.textContent ?? '').trim().slice(0, 40)}"`,
        where: describe(element),
      });
    }
  }
  return found;
}

/** Code shown to a person: snake_case, template holes, undefined, NaN, null, long hashes, raw enums. */
export function rawTextCheck(input: CheckInput): Violation[] {
  const patterns: [string, RegExp][] = [
    ['snake_case identifier', /\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b/],
    ['template hole', /\$\{/],
    ['undefined', /\bundefined\b/],
    ['NaN', /\bNaN\b/],
    ['null', /\bnull\b/],
    ['raw hash or id', /\b[0-9a-f]{12,}\b/i],
    ['raw markdown', /`|\*\*/],
  ];
  const found: Violation[] = [];
  for (const {element, text} of textBoxes()) {
    if (inCode(element)) continue;
    const hits = patterns.flatMap(([name, pattern]) => {
      const match = pattern.exec(text);
      return match === null ? [] : [`${name} "${match[0]}"`];
    });
    for (const word of input.rawWords) {
      if (text.includes(word)) hits.push(`enum value "${word}"`);
    }
    for (const hit of hits) {
      found.push({
        check: 'raw-text',
        message: `raw text shown: ${hit} in "${text.slice(0, 50)}"`,
        where: describe(element),
      });
    }
  }
  return found;
}

/** Whether a label or unit (letters, %, ×) sits within three levels around the number's element. */
function hasLabel(element: Element, number: string): boolean {
  let scope: Element | null = element;
  for (let up = 0; up < 3 && scope !== null; up += 1) {
    const rest = (scope.textContent ?? '').replace(number, '');
    if (/[A-Za-z%×]/.test(rest) || scope.hasAttribute('aria-label')) return true;
    scope = scope.parentElement;
  }
  return false;
}

/** A bare number, with no unit of its own and no label in the row it sits in. */
export function numberLabelCheck(): Violation[] {
  const found: Violation[] = [];
  const bare = /^[+-]?\d[\d,.]*$/;
  for (const {element, text} of textBoxes()) {
    if (!bare.test(text) || inCode(element)) continue;
    if (!hasLabel(element, text)) {
      found.push({
        check: 'number-label',
        message: `the number ${text} has no label or unit beside it`,
        where: describe(element),
      });
    }
  }
  return found;
}

type Mark = {color: string; where: string};

/** The mark `element` draws in `color`, or none when it is clear or hidden. */
function markOf(element: Element, color: string): Mark[] {
  return toRgb(color)[3] > 40 && isShown(element) ? [{color, where: describe(element)}] : [];
}

/** The dots, shapes and thick lines one chart draws. */
function marksOfChart(chart: Element): Mark[] {
  const marks: Mark[] = [];
  const svg = chart.getBoundingClientRect();
  for (const element of chart.querySelectorAll('circle, polygon, rect')) {
    const box = element.getBoundingClientRect();
    const background = box.width * box.height > svg.width * svg.height * 0.5;
    if (!background && box.width > 0) {
      marks.push(...markOf(element, getComputedStyle(element).fill));
    }
  }
  for (const element of chart.querySelectorAll('path, polyline')) {
    const style = getComputedStyle(element);
    const thick = style.fill === 'none' && Number.parseFloat(style.strokeWidth) >= 1.5;
    if (thick) marks.push(...markOf(element, style.stroke));
  }
  return marks;
}

/** The marks of every chart: an svg at least 100 by 40 px. */
function chartMarks(): Mark[] {
  return [...document.body.querySelectorAll('svg')].flatMap(chart => {
    const svg = chart.getBoundingClientRect();
    return svg.width < 100 || svg.height < 40 ? [] : marksOfChart(chart);
  });
}

/** The segments of every strip: a row of empty elements that all have a fill. */
function stripMarks(): Mark[] {
  const marks: Mark[] = [];
  for (const parent of document.body.querySelectorAll('*')) {
    const filled = [...parent.children].filter(
      child =>
        (child.textContent ?? '').trim() === '' &&
        child.children.length === 0 &&
        toRgb(getComputedStyle(child).backgroundColor)[3] > 40,
    );
    if (filled.length < 2 || filled.length !== parent.children.length) continue;
    for (const child of filled)
      marks.push(...markOf(child, getComputedStyle(child).backgroundColor));
  }
  return marks;
}

/** The colors the screen draws marks in (chart dots, steps, strip segments), with who draws them. */
function markColors(): Mark[] {
  return [...chartMarks(), ...stripMarks()];
}

/** The colors a legend item (a small swatch beside a text) names on this screen. */
function legendColors(): string[] {
  const colors: string[] = [];
  for (const element of document.body.querySelectorAll('*')) {
    const box = element.getBoundingClientRect();
    if (box.width === 0 || box.width > 24 || box.height > 24 || !isShown(element)) continue;
    const style = getComputedStyle(element);
    const swatch = element instanceof SVGElement ? style.fill : style.backgroundColor;
    const label = (element.parentElement?.textContent ?? '').replace(/\s+/g, '').length > 0;
    if (label && toRgb(swatch)[3] > 40) colors.push(swatch);
  }
  return colors;
}

/** A color that marks draw but no legend item on the screen names. */
export function legendCheck(): Violation[] {
  const legend = legendColors();
  const reported = new Set<string>();
  const found: Violation[] = [];
  for (const {color, where} of markColors()) {
    const [r, g, b] = toRgb(color);
    const key = `${Math.round(r / 24)}-${Math.round(g / 24)}-${Math.round(b / 24)}`;
    if (reported.has(key) || legend.some(item => sameColor(item, color))) continue;
    reported.add(key);
    found.push({
      check: 'legend',
      message: `the color rgb(${r}, ${g}, ${b}) is drawn but no legend item names it`,
      where,
    });
  }
  return found;
}

/** Struck-through text: it reads as deleted, so no state or count is ever shown that way. */
export function strikeCheck(): Violation[] {
  const found: Violation[] = [];
  for (const {element, text} of textBoxes()) {
    for (let up: Element | null = element; up !== null; up = up.parentElement) {
      if (getComputedStyle(up).textDecorationLine.includes('line-through')) {
        found.push({
          check: 'strike',
          message: `text is struck through and reads as deleted: "${text.slice(0, 40)}"`,
          where: describe(element),
        });
        break;
      }
    }
  }
  return found;
}

/** The performance chart drawn too small to read: under 480 by 80 px, its labels are specks. */
export function chartSizeCheck(): Violation[] {
  const found: Violation[] = [];
  const [minWidth, minHeight] = [480, 80];
  const charts = document.body.querySelectorAll(
    'svg[aria-label*="of each measured idea over the run"]',
  );
  for (const chart of charts) {
    const {width, height} = chart.getBoundingClientRect();
    if (isShown(chart) && (width < minWidth || height < minHeight)) {
      found.push({
        check: 'chart-size',
        message: `the performance chart is ${Math.round(width)} by ${Math.round(height)} px, under the ${minWidth} by ${minHeight} px it needs to be read`,
        where: describe(chart),
      });
    }
  }
  return found;
}

/** The child of `body` that holds `element`; null when `element` is not inside `body`. */
function columnOf(body: Element, element: Element): Element | null {
  let column: Element | null = element;
  while (column !== null && column.parentElement !== body) column = column.parentElement;
  return column;
}

/**
 * A pane (`.vs-pane-body`) whose content column is narrower than the pane while its text wraps:
 * the user sees a block of text with dead space beside it. The column is the body's own child
 * that holds the wrapped text (a narrative, a card, a log); it must cover 85% of the body's inner
 * width. Text that fits on single lines wants no more room, and a paragraph inside a full-width
 * column may still be capped by its own line length: only the column is checked.
 */
export function paneFillCheck(): Violation[] {
  const found: Violation[] = [];
  const share = 0.85;
  const wrapped = textBoxes().filter(box => !inCode(box.element) && lineCount(box.node) > 1);
  for (const body of document.querySelectorAll('.vs-pane-body')) {
    const style = getComputedStyle(body);
    const inner = body.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const reported = new Set<Element>();
    for (const box of wrapped) {
      const column = columnOf(body, box.element);
      if (column === null || reported.has(column)) continue;
      const used = column.getBoundingClientRect().width;
      if (inner <= 0 || used >= inner * share) continue;
      reported.add(column);
      found.push({
        check: 'pane-fill',
        message: `a pane's content uses ${Math.round(used)} px of its ${Math.round(inner)} px (${Math.round((100 * used) / inner)}%) while text wraps: "${box.text.slice(0, 40)}"`,
        where: describe(column),
      });
    }
  }
  return found;
}

/** What `screen-check.ts` sends to the page ahead of a check. */
export const HELPERS: readonly ((...args: never[]) => unknown)[] = [
  inCode,
  describe,
  boxOf,
  cut,
  drawnOver,
  hasLabel,
  clipArea,
  isShown,
  textBoxes,
  lineCount,
  brokenWords,
  toRgb,
  sameColor,
  markOf,
  marksOfChart,
  chartMarks,
  stripMarks,
  markColors,
  legendColors,
  columnOf,
];
