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
  | 'left-edge'
  | 'heading-depth'
  | 'spacing-order'
  | 'hollow-box'
  | 'title-strip'
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

/** The widest a paragraph may run at `fontSize`: the line cap, about 105 characters of half an em. */
function proseCap(fontSize: number): number {
  return 105 * 0.5 * fontSize;
}

/**
 * A pane (`.vs-pane-body`) whose content column squeezes its text: the text wraps while room beside
 * it is empty, and the column is shorter than a full line of prose. The column is the body's own
 * child that holds the wrapped text (a narrative, a card, a log); it must cover 85% of the body's
 * inner width or reach the line cap (`proseCap`), so a card that fits its content or a paragraph
 * at the cap passes however wide the pane is. Text that fits on single lines wants no more room.
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
      const cap = proseCap(Number.parseFloat(getComputedStyle(box.element).fontSize));
      if (inner <= 0 || used >= Math.min(inner * share, cap)) continue;
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

/** The bottom edge, in the window, of what `element` may fill: its nearest clipping ancestor, else the window. */
function roomBottom(element: Element): number {
  for (let up = element.parentElement; up !== null; up = up.parentElement) {
    const style = getComputedStyle(up);
    if (style.overflowY !== 'visible') {
      return (
        up.getBoundingClientRect().bottom -
        Number.parseFloat(style.paddingBottom) -
        Number.parseFloat(style.borderBottomWidth)
      );
    }
  }
  return innerHeight;
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

/** Whether a `style` draws its border on `side`. */
function drawsSide(style: CSSStyleDeclaration, side: 'Top' | 'Right' | 'Bottom' | 'Left'): boolean {
  return (
    Number.parseFloat(style[`border${side}Width`]) > 0 &&
    style[`border${side}Style`] !== 'none' &&
    toRgb(style[`border${side}Color`])[3] > 40
  );
}

/** The color drawn behind `element`: the nearest fill among what holds it. */
function backdropOf(element: Element): string {
  for (let up = element.parentElement; up !== null; up = up.parentElement) {
    const fill = getComputedStyle(up).backgroundColor;
    if (toRgb(fill)[3] > 40) return fill;
  }
  return 'rgb(255, 255, 255)';
}

/** Whether `element` draws a box a person sees: a border on three sides or more, or its own fill. */
function drawsBox(element: Element): boolean {
  const style = getComputedStyle(element);
  const sides = (['Top', 'Right', 'Bottom', 'Left'] as const).filter(each =>
    drawsSide(style, each),
  );
  if (sides.length >= 3) return true;
  const fill = style.backgroundColor;
  return (
    fill !== 'rgba(0, 0, 0, 0)' && toRgb(fill)[3] > 40 && !sameColor(fill, backdropOf(element))
  );
}

interface Ink {
  readonly element: Element;
  readonly box: Box;
}

/** What the screen draws: its text lines, its pictures and controls, and its boxes. */
function screenInk(texts: readonly TextBox[]): Ink[] {
  const ink: Ink[] = texts.flatMap(({element, lines}) => lines.map(box => ({element, box})));
  const pictures = 'svg, img, canvas, input, select, textarea, [role="progressbar"]';
  for (const element of document.body.querySelectorAll('*')) {
    if (!(element.matches(pictures) || drawsBox(element)) || !isShown(element)) continue;
    const box = boxOf(element.getBoundingClientRect());
    if (box.right > box.left && box.bottom > box.top) ink.push({element, box});
  }
  return ink;
}

/** What `root` holds of `ink`, itself not included. */
function inkIn(root: Element, ink: readonly Ink[]): Box[] {
  return ink.filter(each => each.element !== root && root.contains(each.element)).map(e => e.box);
}

/** The smallest box around `boxes`. */
function extentOf(boxes: readonly Box[]): Box | null {
  if (boxes.length === 0) return null;
  return {
    left: Math.min(...boxes.map(box => box.left)),
    top: Math.min(...boxes.map(box => box.top)),
    right: Math.max(...boxes.map(box => box.right)),
    bottom: Math.max(...boxes.map(box => box.bottom)),
  };
}

interface PageParts {
  readonly head: Element;
  readonly body: Element;
  /** What holds them both: a child of it sits under the page's title. */
  readonly page: Element;
}

/** Each page on screen: its header (title, back) and the body under it. */
function pagesOnScreen(): PageParts[] {
  const parts: PageParts[] = [];
  for (const page of document.body.querySelectorAll('.vs-screen, .vs-page-content')) {
    const head = page.querySelector(':scope > .vs-screen-header, :scope > .vs-page-header');
    const body = page.querySelector(
      ':scope > .vs-screen-body, :scope > .vs-page-scroll > .vs-page-body',
    );
    if (head !== null && body !== null && isShown(head)) parts.push({head, body, page});
  }
  return parts;
}

/** The title of a page's header: its heading, else the first text in it. */
function titleOf(head: Element, texts: readonly TextBox[]): TextBox | null {
  const inside = texts.filter(each => head.contains(each.element));
  const heading = inside.find(each => each.element.closest('h1, h2, h3') !== null);
  return heading ?? inside[0] ?? null;
}

/**
 * Law 1 of the visual spec: a screen has one left edge. The back control, the title and the
 * content start at one x, to within 1 px. What is compared is what is drawn, so a borderless back
 * button counts from its icon, not from the padding around it, on the title's line or above it,
 * and a title counts from where it starts even with a back control before it.
 */
export function leftEdgeCheck(): Violation[] {
  const found: Violation[] = [];
  const texts = textBoxes();
  const ink = screenInk(texts);
  for (const {head, body} of pagesOnScreen()) {
    const title = titleOf(head, texts);
    const line = title?.lines[0];
    const content = extentOf(inkIn(body, ink));
    if (title === null || line === undefined || content === null) continue;
    const onLine = inkIn(head, ink).filter(box => box.top < line.bottom && box.bottom > line.top);
    const back = [...head.querySelectorAll('[aria-label^="Back"]')].flatMap(each =>
      inkIn(each, ink),
    );
    const first = extentOf(back.length > 0 ? back : onLine)?.left ?? line.left;
    const edges: readonly [string, number][] = [
      ['back control', first],
      ['title', line.left],
    ];
    for (const [name, edge] of edges) {
      if (Math.abs(edge - content.left) <= 1 || (name === 'back control' && edge === line.left)) {
        continue;
      }
      found.push({
        check: 'left-edge',
        message: `the ${name} of "${title.text.slice(0, 30)}" starts at x ${Math.round(edge)} and the content at x ${Math.round(content.left)}: one left edge is shared`,
        where: describe(head),
      });
    }
  }
  return found;
}

/** The size text is drawn at. */
function textSize(box: TextBox): number {
  return Number.parseFloat(getComputedStyle(box.element).fontSize);
}

/** The level of a heading: the number of its tag, or its `aria-level`. */
function headingLevel(element: Element): number {
  const level = element.getAttribute('aria-level') ?? element.tagName.slice(1);
  return Number.parseInt(level, 10) || 2;
}

/** The headings on screen under one that outranks them and is drawn smaller. */
function headingsLargerThanParent(texts: readonly TextBox[]): Violation[] {
  const found: Violation[] = [];
  const open: {level: number; size: number; text: string}[] = [];
  for (const box of texts) {
    if (!box.element.matches('h1, h2, h3, h4, h5, h6, [role="heading"]')) continue;
    const level = headingLevel(box.element);
    const size = textSize(box);
    while ((open.at(-1)?.level ?? 0) >= level) open.pop();
    const parent = open.at(-1);
    if (parent !== undefined && size > parent.size) {
      found.push({
        check: 'heading-depth',
        message: `the heading "${box.text.slice(0, 30)}" is ${size} px, larger than the ${parent.size} px of "${parent.text.slice(0, 30)}" above it`,
        where: describe(box.element),
      });
    }
    open.push({level, size, text: box.text});
  }
  return found;
}

/**
 * Law 2 of the visual spec: size follows depth. A page's title is its one largest text, so nothing
 * under it is as large as it is; and a heading is never larger than the heading that outranks it.
 * A label set small over larger rows inside a section is not read as a level here: the title
 * and the headings are what the page declares its depth with.
 */
export function headingDepthCheck(): Violation[] {
  const found: Violation[] = [];
  const texts = textBoxes();
  for (const {head, page} of pagesOnScreen()) {
    const title = titleOf(head, texts);
    if (title === null) continue;
    const others = texts.filter(box => page.contains(box.element) && box !== title);
    const largest = [...others].sort((a, b) => textSize(b) - textSize(a))[0];
    if (largest === undefined || textSize(largest) < textSize(title)) continue;
    found.push({
      check: 'heading-depth',
      message: `the title "${title.text.slice(0, 30)}" is ${textSize(title)} px and a text under it is ${textSize(largest)} px: "${largest.text.slice(0, 30)}"`,
      where: describe(title.element),
    });
  }
  return [...found, ...headingsLargerThanParent(texts)];
}

interface Row {
  readonly element: Element;
  readonly rect: DOMRect;
}

/** The children of `element` when they stack top to bottom, in order; null when they do not. */
function stackedRows(element: Element): Row[] | null {
  const rows = drawnChildren(element)
    .filter(child => {
      const position = getComputedStyle(child).position;
      return position !== 'absolute' && position !== 'fixed' && isShown(child);
    })
    .map(child => ({element: child, rect: child.getBoundingClientRect()}))
    .filter(row => row.rect.width > 0 && row.rect.height > 0)
    .sort((a, b) => a.rect.top - b.rect.top);
  if (rows.length < 2) return null;
  const stacked = rows.every((row, at) => {
    const before = rows[at - 1];
    return before === undefined || row.rect.top >= before.rect.bottom - 1;
  });
  return stacked ? rows : null;
}

/** The widest gap between the rows of the group `element` is, seen through a lone wrapper; else 0. */
function gapInside(element: Element): number {
  if (drawsBox(element)) return 0;
  const rows = stackedRows(element);
  if (rows === null) {
    const [only, ...rest] = drawnChildren(element);
    return only !== undefined && rest.length === 0 ? gapInside(only) : 0;
  }
  const gaps = rows.slice(1).map((row, at) => row.rect.top - (rows[at]?.rect.bottom ?? 0));
  return Math.max(0, ...gaps);
}

/** What is wrong with the gap between two stacked neighbours, or null when it shows their groups. */
function spacingFault(before: Row, row: Row): string | null {
  if (
    drawsSide(getComputedStyle(before.element), 'Bottom') ||
    drawsSide(getComputedStyle(row.element), 'Top')
  ) {
    return null;
  }
  const gap = row.rect.top - before.rect.bottom;
  const inside = Math.max(gapInside(before.element), gapInside(row.element));
  if (inside <= 0 || gap > inside) return null;
  return `${Math.round(gap)} px between groups is not more than the ${Math.round(inside)} px inside one: "${(before.element.textContent ?? '').trim().slice(0, 24)}" and "${(row.element.textContent ?? '').trim().slice(0, 24)}"`;
}

/**
 * Law 3 of the visual spec: spacing shows relationships. Two neighbours stacked in a column, one of
 * them a group of rows: the gap between them is larger than the gap inside the group, or the eye
 * reads the wrong grouping. A group with a drawn box, or a line drawn between the two, has its edge
 * to show that. A page's header and body are the page, not groups of it, and an agent's own rendered
 * prose is not the app's layout.
 */
export function spacingOrderCheck(): Violation[] {
  const found: Violation[] = [];
  for (const parent of document.body.querySelectorAll('*')) {
    if (parent.closest('.vs-markdown, svg, code, pre') !== null) continue;
    if (parent.matches('.vs-screen, .vs-page-content')) continue;
    const rows = stackedRows(parent);
    if (rows === null || !isShown(parent)) continue;
    const pairs = rows.slice(1).map((row, at) => [rows[at] ?? row, row] as const);
    for (const [before, row] of pairs) {
      const message = spacingFault(before, row);
      if (message !== null) found.push({check: 'spacing-order', message, where: describe(parent)});
    }
  }
  return found;
}

/**
 * Whether `element` is a box that may be hollow: a drawn box wide enough for its width to show,
 * holding text. Not a control or a row of a navigator (what they hold is a label and they are as wide as their
 * list); not a pane or a log, which the frame sizes; not a table or a data grid, which fill.
 */
function canBeHollow(element: Element, width: number): boolean {
  const never =
    'button, a, label, input, select, textarea, nav, dialog, svg, pre, [role="option"], [role="tab"], .vs-pane';
  const grid = 'table, [role="grid"], [role="table"]';
  const waiting = element.matches('[role="progressbar"]:not([aria-valuenow])');
  return (
    width >= 400 &&
    element.closest(never) === null &&
    element.closest(grid) === null &&
    element.querySelector(grid) === null &&
    isShown(element) &&
    (waiting || ((element.textContent ?? '').trim() !== '' && drawsBox(element)))
  );
}

/** Whether all of `element` is drawn: what is scrolled or clipped away cannot be measured. */
function fullyDrawn(element: Element, rect: DOMRect): boolean {
  const window: Box = {left: 0, top: 0, right: innerWidth, bottom: innerHeight};
  const box = boxOf(rect);
  const drawn = [cut(box, window), cut(box, clipArea(element.parentElement ?? element) ?? box)];
  return drawn.every(
    each =>
      each !== null &&
      each.bottom - each.top >= rect.height - 1 &&
      each.right - each.left >= rect.width - 1,
  );
}

/** The width `element` keeps for content: inside its border and padding. */
function contentWidth(element: Element): number {
  const style = getComputedStyle(element);
  const insets = [
    style.paddingLeft,
    style.paddingRight,
    style.borderLeftWidth,
    style.borderRightWidth,
  ];
  const used = insets.reduce((sum, inset) => sum + Number.parseFloat(inset), 0);
  return element.getBoundingClientRect().width - used;
}

/**
 * Law 4 of the visual spec: a box fits its content, and law 5: a state is no larger than what it
 * stands in for. A drawn box whose content, from its first mark to its last, spans under 40% of the
 * width inside it: three short lines in a full-width card. A row with a name at one end and an
 * action at the other spans its width. Height is not read: a box's neighbours set it. A box cut by
 * the window or a scroller is left out, since what it holds out of sight cannot be measured.
 */
export function hollowBoxCheck(): Violation[] {
  const found: Violation[] = [];
  const share = 0.4;
  const ink = screenInk(textBoxes());
  for (const element of document.body.querySelectorAll('*')) {
    const rect = element.getBoundingClientRect();
    if (!canBeHollow(element, rect.width) || !fullyDrawn(element, rect)) continue;
    const room = contentWidth(element);
    const extent = extentOf(inkIn(element, ink));
    const used = extent === null ? 0 : extent.right - extent.left;
    if (room <= 0 || used >= room * share) continue;
    found.push({
      check: 'hollow-box',
      message: `a box ${Math.round(rect.width)} px wide holds ${Math.round(used)} px of content (${Math.round((100 * used) / room)}%), under the ${100 * share}% a box fits: "${(element.textContent || element.getAttribute('aria-label') || '').trim().slice(0, 40)}"`,
      where: describe(element),
    });
  }
  return found;
}

/**
 * Law 7 of the visual spec: a band earns its height. The window's own controls (the traffic lights)
 * are not in the page; `--chrome-inset-height` says how much room they take. When the page draws
 * nothing in the top 24 px of the window (a control is a 12 px disc, so 24 px is its row with room
 * to spare), that strip carries only the window's controls and the room is spent for nothing: the
 * first text, picture, control or box starts lower. A window with no controls has no such strip.
 */
export function titleStripCheck(): Violation[] {
  const controls = Number.parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue('--chrome-inset-height'),
  );
  const row = 24;
  if (!(controls > 0)) return [];
  const tops = screenInk(textBoxes())
    .filter(each => each.box.bottom > 0 && each.box.top < innerHeight)
    .map(each => Math.max(0, each.box.top));
  if (tops.length === 0) return [];
  const first = Math.min(...tops);
  if (first <= row) return [];
  return [
    {
      check: 'title-strip',
      message: `the top ${Math.round(first)} px of the window carry only its own controls: the first text, picture or control starts at y ${Math.round(first)}, past the ${row} px a row of controls needs`,
      where: 'window',
    },
  ];
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
  proseCap,
  drawsSide,
  backdropOf,
  drawsBox,
  screenInk,
  inkIn,
  extentOf,
  pagesOnScreen,
  titleOf,
  textSize,
  headingLevel,
  headingsLargerThanParent,
  stackedRows,
  gapInside,
  spacingFault,
  canBeHollow,
  fullyDrawn,
  contentWidth,
];
