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
  | 'repeat'
  | 'chart-size'
  | 'pane-fill'
  | 'scroll-room'
  | 'control-row'
  | 'column-tops'
  | 'truncation-room'
  | 'focus-ring'
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
  /** The ids the backend registries name (harnesses, models, roles): none may be drawn as text. */
  readonly ids: readonly string[];
  /** The names the registries give them: none may be drawn in lowercase. */
  readonly names: readonly string[];
  /** Names the user typed (a folder, an SSH alias): identifiers of theirs, not the app's code names. */
  readonly userNames: readonly string[];
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
  let below: Element | null = null;
  for (let up: Element | null = element; up !== null; up = up.parentElement) {
    // What a closed disclosure holds is not drawn, though its text still has a box.
    if (up instanceof HTMLDetailsElement && !up.open && below?.tagName !== 'SUMMARY') return false;
    below = up;
    const style = getComputedStyle(up);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
      return false;
    }
  }
  return true;
}

/** A native modal is painted above its background, so their boxes may overlap. */
function sameDialog(a: Element, b: Element): boolean {
  return a.closest('dialog:modal') === b.closest('dialog:modal');
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
      if (
        a.box.node === b.box.node ||
        !sameDialog(a.box.element, b.box.element) ||
        !drawnOver(a.line, b.line)
      )
        continue;
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

/**
 * The labels in a text: its parts between the separators the screens join labels with (`·`, a
 * comma, a colon, parentheses), so a code name standing as a label is told from a hyphenated
 * word inside a sentence ("per-slot sequence numbers").
 */
function labelsOf(text: string): string[] {
  return text
    .split(/\s[·•|/+]\s|,\s|:\s|\s\(|\)/)
    .map(part => part.trim())
    .filter(part => part !== '');
}

/** Whether `text` has `id` standing as its own word, not inside a longer identifier. */
function hasIdAsWord(text: string, id: string): boolean {
  const escaped = id.replace(/[.*+?^${}()|[\]\\/-]/g, '\\$&');
  return new RegExp(`(?<![\\w.\\-/])${escaped}(?![\\w\\-/]|\\.\\w)`).test(text);
}

/** The registry ids that stand as a label of `text`: a label is a few words, a longer run is a sentence. */
function idHits(text: string, input: CheckInput): string[] {
  const labels = labelsOf(text).filter(label => label.split(' ').length <= 4);
  return input.ids
    .filter(id => labels.some(label => hasIdAsWord(label, id)))
    .map(id => `registry id "${id}"`);
}

/** The labels of `text` that are a kebab-case code name, or a registry name drawn in lowercase. */
function nameHits(text: string, input: CheckInput): string[] {
  const lowered = new Set(input.names.map(name => name.toLowerCase()));
  return labelsOf(text).flatMap(label => {
    if (/^[a-z]+(?:-[a-z0-9]+)+$/.test(label)) {
      return input.userNames.includes(label) ? [] : [`code name "${label}"`];
    }
    return lowered.has(label) && !input.names.includes(label) ? [`lowercase name "${label}"`] : [];
  });
}

/**
 * Code shown to a person: snake_case, template holes, undefined, NaN, null, long hashes, raw enums,
 * and the code names of the registries: a harness, model or role id drawn as text, a kebab-case
 * identifier standing as a label (`opus-5-5`), and a registry name drawn in lowercase as a label
 * (`claude code`). Ids are matched in labels (at most four words between separators), not in
 * sentences. Genuine identifiers a person may copy sit in a `data-mono-id` element.
 */
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
    const hits = [
      ...patterns.flatMap(([name, pattern]) => {
        const match = pattern.exec(text);
        return match === null ? [] : [`${name} "${match[0]}"`];
      }),
      ...input.rawWords.filter(word => text.includes(word)).map(word => `enum value "${word}"`),
      ...idHits(text, input),
      // An agent's own prose (rendered markdown) lists hyphenated words between commas: not labels.
      ...(element.closest('.vs-markdown') === null ? nameHits(text, input) : []),
    ];
    for (const hit of [...new Set(hits)]) {
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

/**
 * A list row that says its status twice: the line under its title only repeats a chip on the same
 * row (a `Passed` line beside a `Passed` badge). The line is for what the chip does not say.
 */
export function repeatCheck(): Violation[] {
  const found: Violation[] = [];
  for (const row of document.querySelectorAll('.vs-pickrow')) {
    const text = row.querySelector('.vs-pickrow-text');
    const line = text?.children[1]?.textContent?.trim().toLowerCase();
    if (line === undefined || line === '') continue;
    for (const chip of row.querySelectorAll('.vs-chip')) {
      if (chip.textContent?.trim().toLowerCase() !== line) continue;
      found.push({
        check: 'repeat',
        message: `a row says "${chip.textContent?.trim()}" twice: as its badge and under its title`,
        where: describe(row),
      });
    }
  }
  return found;
}

/** What is wrong with the labels of one chart: drawn under `minText` px on screen, or cut by its edge. */
function chartLabelProblems(chart: Element, minText: number): string[] {
  const frame = chart.getBoundingClientRect();
  const view = (chart as SVGSVGElement).viewBox.baseVal;
  const scale = view.width > 0 ? frame.width / view.width : 1;
  const problems: string[] = [];
  for (const label of chart.querySelectorAll('text')) {
    const text = (label.textContent ?? '').trim();
    if (text === '') continue;
    const size = Number.parseFloat(getComputedStyle(label).fontSize) * scale;
    const box = label.getBoundingClientRect();
    if (size < minText) {
      problems.push(
        `the chart label "${text}" is drawn ${Math.round(size)} px high, under ${minText} px`,
      );
    }
    if (box.left < frame.left - 1 || box.right > frame.right + 1) {
      problems.push(`the chart label "${text}" is cut by the edge of the drawing`);
    }
  }
  return problems;
}

/**
 * A chart (the performance chart, the trade-off chart) that cannot be read: under 280 by 100 px,
 * or with a label that is drawn under 10 px high on screen (a drawing scaled down to fit) or
 * that the drawing's edge cuts.
 */
export function chartSizeCheck(): Violation[] {
  const found: Violation[] = [];
  const [minWidth, minHeight] = [280, 100];
  for (const chart of document.body.querySelectorAll('svg.vs-chart, .vs-pareto > svg')) {
    if (!isShown(chart)) continue;
    const {width, height} = chart.getBoundingClientRect();
    const problems = chartLabelProblems(chart, 10);
    if (width < minWidth || height < minHeight) {
      problems.unshift(
        `the chart is ${Math.round(width)} by ${Math.round(height)} px, under the ${minWidth} by ${minHeight} px it needs to be read`,
      );
    }
    for (const message of problems) {
      found.push({check: 'chart-size', message, where: describe(chart)});
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

/** The children of `element` as drawn: a child with `display: contents` draws its own children instead. */
function drawnChildren(element: Element): Element[] {
  return [...element.children].flatMap(child =>
    getComputedStyle(child).display === 'contents' ? drawnChildren(child) : [child],
  );
}

/**
 * The bottom edge, in the window, of what `element` may fill: its nearest clipping ancestor, else
 * the window, less the content drawn under it on the way there (with the gap above that content).
 * Content is never room; the free space after it is.
 */
function roomBottom(element: Element): number {
  let used = 0;
  let inner = element;
  for (let up = element.parentElement; up !== null; inner = up, up = up.parentElement) {
    const style = getComputedStyle(up);
    const bottom = inner.getBoundingClientRect().bottom;
    const under = drawnChildren(up)
      .filter(child => !['absolute', 'fixed'].includes(getComputedStyle(child).position))
      .map(child => child.getBoundingClientRect())
      .filter(rect => rect.height > 0 && rect.top >= bottom - 0.5);
    if (under.length > 0)
      used +=
        Math.max(...under.map(rect => rect.bottom)) -
        Math.min(...under.map(rect => rect.top)) +
        (Number.parseFloat(style.rowGap) || 0);
    if (style.overflowY !== 'visible') {
      return (
        up.getBoundingClientRect().bottom -
        Number.parseFloat(style.paddingBottom) -
        Number.parseFloat(style.borderBottomWidth) -
        used
      );
    }
  }
  return innerHeight - used;
}

/** The height of `column` that its drawn children do not use, plus what lies below it in what holds it. */
function columnRoom(column: Element): number {
  const style = getComputedStyle(column);
  const rows = drawnChildren(column)
    .map(child => child.getBoundingClientRect())
    .sort((a, b) => a.top - b.top);
  const gap = Number.parseFloat(style.rowGap) || 0;
  const bottom = column.getBoundingClientRect().bottom;
  const inner =
    bottom - Number.parseFloat(style.paddingBottom) - Number.parseFloat(style.borderBottomWidth);
  let room = Math.max(0, roomBottom(column) - bottom);
  for (const [index, rect] of rows.entries()) {
    const below = rows[index + 1]?.top ?? inner;
    room += below - rect.bottom - (index + 1 < rows.length ? gap : 0);
  }
  return room;
}

/**
 * A region that scrolls vertically while room is left to show more of it: its rows are cut
 * although empty space sits around it. The room is the height of its column that no child
 * uses (between the drawn children, in the order drawn, and after the last; a fixed margin counts
 * as room too), plus what lies below the column up to the edge of what holds it. A column that is
 * the page's whole height has none below it.
 */
export function scrollRoomCheck(): Violation[] {
  const found: Violation[] = [];
  for (const element of document.body.querySelectorAll('*')) {
    const scrolls = ['auto', 'scroll'].includes(getComputedStyle(element).overflowY);
    const column = element.parentElement;
    if (!scrolls || column === null || element.scrollHeight <= element.clientHeight + 1) continue;
    const style = getComputedStyle(column);
    if (style.display !== 'flex' || style.flexDirection !== 'column' || !isShown(element)) continue;
    const room = columnRoom(column);
    if (room <= 24) continue;
    found.push({
      check: 'scroll-room',
      message: `a region scrolls (${element.scrollHeight} px of content in ${element.clientHeight} px) while it has ${Math.round(room)} px of room`,
      where: describe(element),
    });
  }
  return found;
}

/** The box a person sees for a text field or select: a search box's frame, else the control. */
function frameOf(control: Element): Element {
  return control.closest('.vs-searchbox') ?? control;
}

/** The lowest element that holds both `a` and `b`. */
function commonAncestor(a: Element, b: Element): Element | null {
  for (let up: Element | null = a; up !== null; up = up.parentElement) {
    if (up.contains(b)) return up;
  }
  return null;
}

/** How many levels `element` sits below `ancestor`. */
function depthBelow(ancestor: Element, element: Element): number {
  let depth = 0;
  for (let up: Element | null = element; up !== null && up !== ancestor; up = up.parentElement) {
    depth += 1;
  }
  return depth;
}

/** Whether two boxes sit next to each other on one line of a flex row `style`. */
function sideBySide(a: DOMRect, b: DOMRect, style: CSSStyleDeclaration): boolean {
  const apart = a.right <= b.left + 1 || b.right <= a.left + 1;
  const shared = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  const flexRow = style.display.includes('flex') && style.flexDirection === 'row';
  return apart && flexRow && shared >= Math.min(a.height, b.height) / 2;
}

interface Boxed {
  readonly control: Element;
  readonly frame: Element;
  readonly box: DOMRect;
}

/** The flex row that holds `a` and `b` side by side, at most two levels down; else null. */
function sharedRow(a: Boxed, b: Boxed): Element | null {
  const row = commonAncestor(a.frame, b.frame);
  if (row === null || !sideBySide(a.box, b.box, getComputedStyle(row))) return null;
  return depthBelow(row, a.frame) > 2 || depthBelow(row, b.frame) > 2 ? null : row;
}

/** By how much two controls of one row are out of line (tops, in px), or null when they line up or are not compared. */
function outOfLine(a: Boxed, b: Boxed): number | null {
  const heights = Math.abs(a.box.height - b.box.height);
  const tops = Math.abs(a.box.top - b.box.top);
  if (heights <= 1 && tops <= 1) return null;
  const button = 'button.vs-button';
  return a.control.matches(button) && b.control.matches(button) && heights > 1 ? null : tops;
}

/**
 * Controls side by side in one flex row, at most two levels down from it (a search box and a sort
 * select in a toolbar; the position, state chip and action of the time bar) that are not the same height or do not start at the same
 * top edge, to within 1 px: they read as misaligned, whatever centers them. Radios, checkboxes and
 * sliders are not boxes of this kind. Buttons of different heights (sizes) are not compared;
 * buttons of one height in a row start at one top, so a reason under one of them hangs below
 * the row (Back and Continue).
 */
export function controlRowCheck(): Violation[] {
  const found: Violation[] = [];
  const kinds = [
    'input:not([type="radio"],[type="checkbox"],[type="range"],[type="hidden"],[type="file"])',
    'select',
    'button.vs-button',
    '.vs-scrubber-head > *',
  ].join(',');
  const boxes: Boxed[] = [...document.body.querySelectorAll(kinds)]
    .filter(isShown)
    .map(control => ({control, frame: frameOf(control)}))
    .map(({control, frame}) => ({control, frame, box: frame.getBoundingClientRect()}))
    .filter(each => each.box.width > 0 && each.box.height > 0);
  for (const [at, a] of boxes.entries()) {
    for (const b of boxes.slice(at + 1)) {
      const row = sharedRow(a, b);
      const tops = row === null ? null : outOfLine(a, b);
      if (row === null || tops === null) continue;
      found.push({
        check: 'control-row',
        message: `controls in one row do not line up: ${describe(a.control)} is ${Math.round(a.box.height)} px high and ${describe(b.control)} ${Math.round(b.box.height)} px, their tops differ by ${Math.round(tops)} px`,
        where: describe(row),
      });
    }
  }
  return found;
}

/** Whether `other` sits to the right of `box` on its line: a column that takes room beside it. */
function takesRoomRight(box: DOMRect, other: Element): boolean {
  const style = getComputedStyle(other);
  if (style.position === 'absolute' || style.position === 'fixed' || !isShown(other)) return false;
  const rect = other.getBoundingClientRect();
  const shared = Math.min(box.bottom, rect.bottom) - Math.max(box.top, rect.top);
  return rect.width > 0 && shared > 0 && rect.left >= box.right - 1;
}

/**
 * The width to the right of `element` on its own line that nothing uses, up to the inner edge of
 * the pane, card or screen that holds it. A text with a column beside it (a sibling of it or of
 * what holds it, to its right) is cut by the width that column leaves, a layout choice, so it has
 * no room.
 */
function roomRight(element: Element): number {
  const holder = element.closest('.vs-pane, .vs-card, .vs-screen, .vs-page, main') ?? document.body;
  for (let up: Element | null = element; up !== null && up !== holder; up = up.parentElement) {
    const box = up.getBoundingClientRect();
    for (let next = up.nextElementSibling; next !== null; next = next.nextElementSibling) {
      if (takesRoomRight(box, next)) return 0;
    }
  }
  const style = getComputedStyle(holder);
  const edge =
    holder.getBoundingClientRect().left +
    holder.clientLeft +
    holder.clientWidth -
    Number.parseFloat(style.paddingRight);
  return edge - element.getBoundingClientRect().right;
}

/**
 * Text cut short with an ellipsis while room is left beside it: a tooltip does not excuse a name
 * cut at one edge of a pane when the other side of its line is empty. Room is at least 40 px that
 * no column on the line uses (`roomRight`).
 */
export function truncationRoomCheck(): Violation[] {
  const found: Violation[] = [];
  const wanted = 40;
  for (const element of document.body.querySelectorAll('*')) {
    const style = getComputedStyle(element);
    const clamped = style.webkitLineClamp !== 'none' && style.webkitLineClamp !== '';
    if (!(clamped || style.textOverflow === 'ellipsis') || !isShown(element)) continue;
    if (element.scrollWidth <= element.clientWidth + 1) continue;
    const room = roomRight(element);
    if (room >= wanted) {
      found.push({
        check: 'truncation-room',
        message: `text is cut with an ellipsis while ${Math.round(room)} px beside it are empty: "${(element.textContent ?? '').trim().slice(0, 40)}"`,
        where: describe(element),
      });
    }
  }
  return found;
}

/** Text that the focus ring of the focused control is drawn over (its outline and offset around it). */
export function focusRingCheck(): Violation[] {
  const active = document.activeElement;
  if (active === null || active === document.body) return [];
  const style = getComputedStyle(active);
  const width = style.outlineStyle === 'none' ? 0 : Number.parseFloat(style.outlineWidth) || 0;
  if (width === 0) return [];
  const reach = width + (Number.parseFloat(style.outlineOffset) || 0);
  const box = active.getBoundingClientRect();
  const ring: Box = {
    left: box.left - reach,
    top: box.top - reach,
    right: box.right + reach,
    bottom: box.bottom + reach,
  };
  const found: Violation[] = [];
  for (const {element, text, lines} of textBoxes()) {
    if (
      !sameDialog(active, element) ||
      active.contains(element) ||
      active.closest('label')?.contains(element)
    )
      continue;
    if (lines.some(line => cut(line, ring) !== null)) {
      found.push({
        check: 'focus-ring',
        message: `the focus ring of ${describe(active)} is drawn over the text "${text.slice(0, 30)}"`,
        where: describe(element),
      });
    }
  }
  return found;
}

/**
 * Where a page has a side column beside its main column (the page's footer, beside its body), their
 * first boxes share a top edge: a side card that starts level with the page's logo while the form
 * starts 200 px lower reads as two unrelated screens.
 */
export function columnTopsCheck(): Violation[] {
  const found: Violation[] = [];
  for (const content of document.body.querySelectorAll('.vs-page-content')) {
    const body = content.querySelector(':scope > .vs-page-scroll > .vs-page-body');
    const side = content.querySelector(':scope > .vs-page-footer');
    const main = body?.firstElementChild;
    const first = side?.firstElementChild;
    if (!body || !side || !main || !first || !isShown(side)) continue;
    const beside = side.getBoundingClientRect().left >= body.getBoundingClientRect().right - 1;
    if (!beside) continue;
    const gap = Math.abs(first.getBoundingClientRect().top - main.getBoundingClientRect().top);
    if (gap <= 2) continue;
    found.push({
      check: 'column-tops',
      message: `the side column starts ${Math.round(gap)} px from the top of the main column`,
      where: describe(first),
    });
  }
  return found;
}

/** What `screen-check.ts` sends to the page ahead of a check. */
export const HELPERS: readonly ((...args: never[]) => unknown)[] = [
  inCode,
  sharedRow,
  outOfLine,
  describe,
  boxOf,
  cut,
  drawnOver,
  hasLabel,
  clipArea,
  isShown,
  sameDialog,
  drawnChildren,
  roomBottom,
  columnRoom,
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
  labelsOf,
  hasIdAsWord,
  idHits,
  nameHits,
  frameOf,
  commonAncestor,
  depthBelow,
  sideBySide,
  takesRoomRight,
  roomRight,
  chartLabelProblems,
];
