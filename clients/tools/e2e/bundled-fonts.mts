import {readFileSync} from 'node:fs';
import type {CDPSession, Page} from '@playwright/test';

/**
 * Whether every glyph the page draws comes from a font the UI library bundles.
 *
 * A glyph that the font in effect lacks is drawn by whatever font the machine falls back to, so
 * the same screen differs between operating systems (the check mark of a step list drew a pixel
 * off on the CI runner and in the image that makes the baselines). Chromium says which font drew
 * each run of text (`CSS.getPlatformFontsForNode`), so the check asks it instead of reading
 * glyph tables. The bundled families are read from `@font-face` in the library's `fonts.css`.
 */

const FONTS_CSS = new URL('../../libs/ui/src/fonts.css', import.meta.url);

/** The families `fonts.css` declares: the only ones a screen may draw with. */
export function bundledFamilies(
  css: string = readFileSync(FONTS_CSS, 'utf8'),
): ReadonlySet<string> {
  const families = new Set<string>();
  for (const face of css.matchAll(/@font-face\s*\{([^}]*)\}/g)) {
    const family = /font-family:\s*"([^"]+)"/.exec(face[1] ?? '');
    if (family?.[1] !== undefined) families.add(family[1]);
  }
  return families;
}

/** One run of text that a font other than the bundled ones drew. */
export interface ForeignFontUse {
  /** The element (or pseudo-element) that holds the text, as `tag#id.class`. */
  readonly where: string;
  /** The start of the text the element holds. */
  readonly text: string;
  /** The font that drew it, and how many glyphs. */
  readonly font: string;
  readonly glyphs: number;
}

interface CdpNode {
  readonly nodeId: number;
  readonly nodeType: number;
  readonly nodeName: string;
  readonly nodeValue?: string;
  readonly attributes?: readonly string[];
  readonly children?: readonly CdpNode[];
  readonly pseudoElements?: readonly CdpNode[];
  readonly shadowRoots?: readonly CdpNode[];
  readonly contentDocument?: CdpNode;
  readonly pseudoType?: string;
}

interface PlatformFont {
  readonly familyName: string;
  readonly isCustomFont: boolean;
  readonly glyphCount: number;
}

const ELEMENT = 1;
const TEXT = 3;
/** Elements whose text the page never draws. */
const UNDRAWN = new Set(['SCRIPT', 'STYLE', 'TITLE', 'NOSCRIPT', 'HEAD']);

function attribute(node: CdpNode, name: string): string {
  const attributes = node.attributes ?? [];
  for (let index = 0; index + 1 < attributes.length; index += 2) {
    if (attributes[index] === name) return attributes[index + 1] ?? '';
  }
  return '';
}

/** The element as `tag#id.class`; inside a shadow tree (the text of a field), after its host. */
function describe(holder: Holder): string {
  const {node, parent, host} = holder;
  const element = node.pseudoType === undefined ? node : (parent ?? node);
  const id = attribute(element, 'id');
  const classes = attribute(element, 'class').split(/\s+/).filter(Boolean);
  const pseudo = node.pseudoType === undefined ? '' : `::${node.pseudoType}`;
  const own = `${element.nodeName.toLowerCase()}${id === '' ? '' : `#${id}`}${classes.map(each => `.${each}`).join('')}${pseudo}`;
  return host === undefined
    ? own
    : `${describe({node: host, parent: undefined, host: undefined})} > ${own}`;
}

/**
 * The characters every bundled face has, as code point ranges: printable ASCII, Latin-1, and the
 * punctuation and arrows the UI writes (dashes, quotes, bullet, ellipsis, minus, arrows). A run
 * made of them cannot fall back, so it is not asked about, which keeps the check to the few
 * nodes that hold a symbol (one browser round trip each; a long run has thousands of `·` and
 * `×`). `bundled-font.spec.ts` draws each of them in both faces, so a range that is wrong fails
 * there.
 */
const COVERED: readonly (readonly [number, number])[] = [
  [0x20, 0x7e],
  [0xa0, 0xff],
  [0x2013, 0x2014],
  [0x2018, 0x2019],
  [0x201c, 0x201d],
  [0x2022, 0x2022],
  [0x2026, 0x2026],
  [0x2190, 0x2193],
  [0x2212, 0x2212],
];

/** Every character of `COVERED`, for the self-test that draws them. */
export const COVERED_CHARACTERS: string = COVERED.flatMap(([from, to]) =>
  Array.from({length: to - from + 1}, (_, index) => String.fromCodePoint(from + index)),
).join('');

function mayFallBack(text: string): boolean {
  for (const character of text) {
    const code = character.codePointAt(0) ?? 0;
    if (!COVERED.some(([from, to]) => code >= from && code <= to)) return true;
  }
  return false;
}

function drawnText(node: CdpNode): string {
  return (node.children ?? [])
    .filter(child => child.nodeType === TEXT)
    .map(child => child.nodeValue ?? '')
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** A node that may draw text: its parent (a pseudo-element's owner) and shadow host, for naming. */
interface Holder {
  readonly node: CdpNode;
  readonly parent: CdpNode | undefined;
  readonly host: CdpNode | undefined;
}

/** Whether the element has text of its own that may need a face a bundled one lacks. */
function drawsText(node: CdpNode): boolean {
  return node.nodeType === ELEMENT && mayFallBack(drawnText(node));
}

/** The nodes inside `node`, each with the shadow host it is under: its children, shadow trees, frame. */
function inside(node: CdpNode, host: CdpNode | undefined): {child: CdpNode; host?: CdpNode}[] {
  const children = (node.children ?? []).map(child => ({child, host}));
  const shadows = (node.shadowRoots ?? []).flatMap(shadow =>
    (shadow.children ?? []).map(child => ({child, host: node})),
  );
  const frame = node.contentDocument === undefined ? [] : [{child: node.contentDocument}];
  return [...children, ...shadows, ...frame];
}

/** Every element and pseudo-element that may draw text, shadow trees (a field's text) included. */
function holders(root: CdpNode): Holder[] {
  const found: Holder[] = [];
  const visit = (node: CdpNode, parent: CdpNode | undefined, host: CdpNode | undefined): void => {
    if (UNDRAWN.has(node.nodeName)) return;
    if (drawsText(node)) found.push({node, parent, host});
    for (const pseudo of node.pseudoElements ?? []) found.push({node: pseudo, parent: node, host});
    for (const each of inside(node, host)) visit(each.child, node, each.host);
  };
  visit(root, undefined, undefined);
  return found;
}

/** A web font reports its face (`JetBrains Mono SemiBold`): the family, then the weight's name. */
function isBundled(face: string, allowed: ReadonlySet<string>): boolean {
  for (const family of allowed) {
    if (face === family || face.startsWith(`${family} `)) return true;
  }
  return false;
}

async function fontsOf(session: CDPSession, nodeId: number): Promise<readonly PlatformFont[]> {
  try {
    const {fonts} = await session.send('CSS.getPlatformFontsForNode', {nodeId});
    return fonts;
  } catch {
    // A node that has no box (display: none) has no fonts to ask about.
    return [];
  }
}

/**
 * The text runs on the page now that a font outside `allowed` drew, so a glyph missing from the
 * bundled font shows up as the system font that stood in for it. Empty when every run is bundled.
 */
export async function foreignFontUses(
  page: Page,
  allowed: ReadonlySet<string> = bundledFamilies(),
): Promise<ForeignFontUse[]> {
  // A face that finished loading only redraws the text at the next layout: wait for the loads in
  // flight, then lay the page out, so the browser reports the fonts the screen shows now.
  await page.evaluate(async () => {
    await document.fonts.ready;
    return document.documentElement.getBoundingClientRect().height;
  });
  const session = await page.context().newCDPSession(page);
  try {
    await session.send('DOM.enable');
    await session.send('CSS.enable');
    const {root} = await session.send('DOM.getDocument', {depth: -1, pierce: true});
    // The requests are independent, so they go out together instead of one round trip each.
    const found = holders(root as CdpNode);
    const fonts = await Promise.all(found.map(({node}) => fontsOf(session, node.nodeId)));
    const foreign: ForeignFontUse[] = [];
    found.forEach((holder, index) => {
      const {node} = holder;
      for (const font of fonts[index] ?? []) {
        if (font.isCustomFont && isBundled(font.familyName, allowed)) continue;
        foreign.push({
          where: describe(holder),
          text:
            node.pseudoType === undefined ? drawnText(node).slice(0, 40) : '(generated content)',
          font: font.isCustomFont ? font.familyName : `${font.familyName} (system)`,
          glyphs: font.glyphCount,
        });
      }
    });
    return foreign;
  } finally {
    await session.detach();
  }
}

/** The failure message of a page with foreign fonts, naming each element, font and the fix. */
export function foreignFontMessage(uses: readonly ForeignFontUse[]): string {
  const lines = uses.map(
    use => `  ${use.where} "${use.text}" drawn by ${use.font} (${use.glyphs} glyphs)`,
  );
  return [
    'text drawn by a font the UI does not bundle (a glyph its font lacks falls back to the machine):',
    ...lines,
    'set font-family to a bundled face (var(--font-mono) has the symbols) or use an icon.',
  ].join('\n');
}
