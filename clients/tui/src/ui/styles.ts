import {
  CodeRenderable,
  createMarkdownCodeBlockRenderer,
  type MarkdownOptions,
  type MarkdownTableOptions,
  type Renderable,
  SyntaxStyle,
} from '@opentui/core';
import bash from 'highlight.js/lib/languages/bash';
import c from 'highlight.js/lib/languages/c';
import cpp from 'highlight.js/lib/languages/cpp';
import diff from 'highlight.js/lib/languages/diff';
import go from 'highlight.js/lib/languages/go';
import ini from 'highlight.js/lib/languages/ini';
import json from 'highlight.js/lib/languages/json';
import python from 'highlight.js/lib/languages/python';
import rust from 'highlight.js/lib/languages/rust';
import yaml from 'highlight.js/lib/languages/yaml';
import {createLowlight} from 'lowlight';
import type {ConversationEntry} from '../session-model.js';
import type {ConversationRole, ConversationRoleColors, Theme} from './theme.js';

export type EntryPalette = ConversationRoleColors;

type MarkdownRenderNode = NonNullable<MarkdownOptions['renderNode']>;

/** What a transcript markdown block is built with, minus its per-entry parts. */
export type MarkdownBlockOptions = Omit<MarkdownOptions, 'content' | 'streaming'>;

interface CodeSurface {
  fg: string;
  bg: string;
}

/**
 * The surface any code draws on, whatever produced it.
 *
 * Two consumers read it, so it has to be one decision. A fence the renderer
 * built as a `CodeRenderable`, top-level or nested in a list, is restyled to it
 * by hand. An inline span, and a nested fence while the renderer coalesces its
 * list away, arrive instead as `markup.raw*` captures inside a markdown block
 * and take their colors from the syntax style. Naming the pair twice lets a
 * fence change color depending on where it sits.
 */
function codeSurface({markdown}: Theme): CodeSurface {
  return {fg: markdown.code, bg: markdown.codeBackground};
}

export function createMarkdownStyle(theme: Theme): SyntaxStyle {
  const {markdown} = theme;
  // Style names are the markup.* capture groups the markdown renderer emits.
  // Lookup tries the exact capture and then only its first dotted segment, so
  // the numbered heading captures each need their own entry: a plain
  // "heading" entry is never consulted, which left every markdown color
  // except default unused.
  const heading = {fg: markdown.heading, bold: true};
  const code = codeSurface(theme);
  return SyntaxStyle.fromStyles({
    default: {fg: markdown.default},
    'markup.heading': heading,
    'markup.heading.1': heading,
    'markup.heading.2': heading,
    'markup.heading.3': heading,
    'markup.heading.4': heading,
    'markup.heading.5': heading,
    'markup.heading.6': heading,
    'markup.strong': {fg: markdown.strong, bold: true},
    'markup.italic': {fg: markdown.em, italic: true},
    'markup.raw': code,
    'markup.raw.block': code,
    'markup.link': {fg: markdown.link, underline: true},
    'markup.link.url': {fg: markdown.link, underline: true},
    'markup.link.label': {fg: markdown.link},
    'markup.quote': {fg: markdown.blockquote, italic: true},
    // The rest are a fenced block's own captures, not markdown's markup.*
    // ones. `SyntaxStyle` falls back from a dotted capture to its first
    // segment (`function.call` -> `function`), so one entry per family
    // covers both the plain vocabulary (bare `keyword`, `string`, as
    // javascript's own highlights.scm emits) and the richer nvim one zig's
    // uses (`punctuation.bracket`, `type.builtin`, `variable.parameter`,
    // ...), and would cover a third source's names too. Anything a grammar
    // captures that is not one of these families (most punctuation-adjacent
    // literals, some grammars' own one-off groups) is intentionally left
    // unmapped: it falls back to `markup.raw.block` via
    // `drawOnCodeSurface`'s `baseHighlight`, which keeps it reading as code
    // instead of prose.
    keyword: {fg: markdown.keyword},
    string: {fg: markdown.string},
    comment: {fg: markdown.comment},
    number: {fg: markdown.number},
    function: {fg: markdown.function},
    type: {fg: markdown.type},
    operator: {fg: markdown.operator},
    variable: {fg: markdown.variable},
    punctuation: {fg: markdown.punctuation},
  });
}

/**
 * Table presentation for markdown blocks.
 *
 * Every option here is one the renderer would otherwise default badly for
 * inside a transcript card. Its own defaults size columns to the available
 * width, which pads a three-character cell out to a quarter of the pane, and
 * they draw the border in a fixed grey that ignores the theme. `content`
 * sizing plus a balanced fitter is what keeps a wide table inside a card that
 * is already inset by the card border and its padding.
 */
export function createMarkdownTableOptions(theme: Theme): MarkdownTableOptions {
  return {
    style: 'grid',
    widthMode: 'content',
    columnFitter: 'balanced',
    wrapMode: 'word',
    cellPaddingX: 1,
    cellPaddingY: 0,
    borders: true,
    borderStyle: 'rounded',
    borderColor: theme.border,
  };
}

/**
 * Declares that a `renderNode` never replaces an ordinary markdown token.
 *
 * `MarkdownRenderable` coalesces runs of prose into one renderable per block,
 * but only while it knows the override cannot claim such a token. Any other
 * `renderNode` drops it to one renderable per token, so a message of a hundred
 * paragraphs becomes a hundred native renderables. The declaration is a marker
 * the renderer stamps on what `createMarkdownCodeBlockRenderer` returns; 0.4.3
 * keeps it off `MarkdownOptions`, so copying it from an empty renderer is the
 * only way to make the claim without naming a private field.
 *
 * The claim holds for the list branch too: while the marker is honored the
 * renderer folds every list into a coalesced markdown block and never offers a
 * `list` token, and the branch returns the renderer's own renderable rather
 * than a replacement whenever it is offered one.
 *
 * The factory itself is not usable directly here: it dispatches per resolved
 * language and drops fences whose info string names none, which is most of
 * them in a transcript.
 */
function asCodeBlockOnly(renderNode: MarkdownRenderNode): MarkdownRenderNode {
  // The factory's return type is the optional `renderNode` field, but it
  // always returns a function.
  const marker = createMarkdownCodeBlockRenderer({}) as MarkdownRenderNode;
  return Object.assign(renderNode, marker);
}

/**
 * Filetypes `@opentui/core` ships a tree-sitter grammar for, mirroring
 * `node_modules/@opentui/core/assets/`. `infoStringToFiletype` (which the
 * renderer already applies before a block reaches `drawOnCodeSurface`, so
 * `block.filetype` is read here rather than re-parsed) happily resolves a
 * name for languages that have no grammar bundled, such as "rust" or "bash",
 * so the filetype alone does not say whether highlighting can run. Only
 * these five are bundled; vendoring more (issue #575 wants C++, Rust, Go,
 * Python) was measured and declined for now - see the module doc comment.
 * `lowlight` below covers a further set of those without a tree-sitter
 * grammar; a filetype in neither set draws flat.
 */
const GRAMMAR_FILETYPES = new Set([
  'javascript',
  'javascriptreact',
  'typescript',
  'typescriptreact',
  'zig',
  'markdown',
  'markdown_inline',
]);

/**
 * Languages with no tree-sitter grammar in `GRAMMAR_FILETYPES`, highlighted
 * instead by running `highlight.js` (via `lowlight`, its hast-producing
 * wrapper) directly over the fence text. Deliberately narrow and one import
 * per language rather than lowlight's `common`/`all` bundle, so a transcript
 * never ships a parser nothing asked for. `registered()` (used below) also
 * matches each grammar's own declared aliases, so `toml` (via `ini`), `sh`/
 * `zsh` (via `bash`), `c++`/`hpp`/... (via `cpp`), `patch` (via `diff`) and
 * `py` (via `python`) resolve too.
 */
const lowlight = createLowlight({bash, c, cpp, diff, go, ini, json, python, rust, yaml});

/** `CodeRenderable.onHighlight`'s type, which `@opentui/core` does not export by name. */
type OnHighlightCallback = NonNullable<CodeRenderable['onHighlight']>;
type HighlightContext = Parameters<OnHighlightCallback>[1];
type SimpleHighlight = Parameters<OnHighlightCallback>[0][number];

/** The nine code families `createMarkdownStyle` registers a color for. */
type HljsFamily =
  | 'keyword'
  | 'string'
  | 'comment'
  | 'number'
  | 'function'
  | 'type'
  | 'operator'
  | 'variable'
  | 'punctuation';

/**
 * Maps a `highlight.js` scope's rendered class name(s) to the family above it
 * reads as. Most scopes are one class; a dotted scope such as "title.function"
 * renders as two ("hljs-title", "function_"), so those two are matched
 * together. `built_in` (a mix of builtin functions and builtin objects across
 * these grammars) and diff's `addition`/`deletion` have no good single family
 * here and are left out, same as any class below with no entry at all.
 */
const HLJS_FAMILY: Record<string, HljsFamily> = {
  'hljs-keyword': 'keyword',
  'hljs-literal': 'keyword',
  'hljs-variable': 'variable',
  'hljs-subst': 'variable',
  'hljs-attr': 'variable',
  'hljs-property': 'variable',
  'hljs-params': 'variable',
  'hljs-type': 'type',
  'hljs-class': 'type',
  'hljs-section': 'type',
  'hljs-string': 'string',
  'hljs-regexp': 'string',
  'hljs-symbol': 'string',
  'hljs-comment': 'comment',
  'hljs-doctag': 'comment',
  'hljs-meta': 'comment',
  'hljs-number': 'number',
  'hljs-operator': 'operator',
  'hljs-punctuation': 'punctuation',
  'hljs-function': 'function',
  'hljs-title function_': 'function',
  'hljs-title class_': 'type',
};

/** The family `className` maps to, matching a two-part scope before a one-part one. */
function hljsFamily(className: Array<string> | undefined): HljsFamily | undefined {
  if (className === undefined || className.length === 0) return undefined;
  return (
    HLJS_FAMILY[className.join(' ')] ??
    (className.length > 2 ? HLJS_FAMILY[className.slice(0, 2).join(' ')] : undefined) ??
    HLJS_FAMILY[className[0] ?? '']
  );
}

/**
 * The `highlight.js` spans for one fence, converted to `SimpleHighlight`s.
 *
 * Walks the hast tree `lowlight.highlight` returns, giving each text node the
 * family of its nearest ancestor `hljsFamily` maps; an ancestor it does not
 * map keeps its own parent's (a bare "title" wrapping a "function", say), and
 * text under no mapped ancestor at all emits nothing, leaving it to
 * `baseHighlight`. That walk only ever grows one running offset across
 * sibling and child text nodes in document order, so the spans it emits are
 * already non-overlapping. Offsets are plain string indices - `content.slice`
 * is what `treeSitterToTextChunks` uses to cash them in, and lowlight's own
 * `.length` walk is over the same (UTF-16) string, so the two already agree.
 */
function lowlightHighlights(filetype: string, content: string): SimpleHighlight[] {
  const root = lowlight.highlight(filetype, content);
  const highlights: SimpleHighlight[] = [];
  let offset = 0;
  const walk = (node: (typeof root.children)[number], family: HljsFamily | undefined): void => {
    if (node.type === 'text') {
      if (family !== undefined && node.value.length > 0) {
        highlights.push([offset, offset + node.value.length, family]);
      }
      offset += node.value.length;
      return;
    }
    if (node.type !== 'element') return;
    const nextFamily = hljsFamily(node.properties.className) ?? family;
    for (const child of node.children) walk(child, nextFamily);
  };
  for (const child of root.children) walk(child, undefined);
  return highlights;
}

/** `CodeRenderable.onHighlight` for a filetype lowlight covers instead of tree-sitter. */
function highlightWithLowlight(
  _highlights: SimpleHighlight[],
  {content, filetype}: HighlightContext,
) {
  return lowlightHighlights(filetype, content);
}

const probedFiletypes = new Set<string>();

/**
 * Reads the `warning`/`error` a oneshot highlight can carry, which
 * `CodeRenderable.startHighlight()` discards (`result.highlights ?? []`,
 * nothing else read off the result) before it ever reaches `onHighlight` or
 * any other public hook: an unsupported filetype and a grammar that failed
 * to load both fall through to the same silent plain text. Probed once per
 * filetype rather than per block or per render pass: which of those two a
 * filetype is stays constant, so a second `highlightOnce` per fence would
 * only repeat the same answer.
 *
 * Quiet by design: `console.debug` for "no parser", which is the expected
 * case for anything in neither `GRAMMAR_FILETYPES` nor `lowlight` (ruby,
 * elixir, ... - see the module doc comment), and `console.warn` for an actual
 * error, which means one of the five bundled grammars is broken. Neither
 * renders anything; this is a developer signal, not a UI banner. Never called
 * for a filetype `lowlight` covers: tree-sitter having no parser for it is
 * expected there too, but `drawOnCodeSurface` already has a substitute, so
 * probing for it would only be noise.
 *
 * ponytail: a grammar that loads fine but fails on one specific fence's
 * content is not caught after the first probe for that filetype. Widen to
 * probing per content if that turns out to matter.
 */
function warnIfHighlightSignalsTrouble(block: CodeRenderable): void {
  const {filetype, content, treeSitterClient} = block;
  if (filetype === undefined || probedFiletypes.has(filetype)) return;
  probedFiletypes.add(filetype);
  void treeSitterClient.highlightOnce(content, filetype).then(({warning, error}) => {
    if (warning) console.debug(`[code highlight] ${filetype}: ${warning}`);
    if (error) console.warn(`[code highlight] ${filetype}: ${error}`);
  });
}

/** Puts one block the renderer already built on the code surface. */
function drawOnCodeSurface(block: CodeRenderable, {fg, bg}: CodeSurface): void {
  block.bg = bg;
  const {filetype} = block;
  const hasGrammar = filetype !== undefined && GRAMMAR_FILETYPES.has(filetype);
  const lowlightCovers = filetype !== undefined && !hasGrammar && lowlight.registered(filetype);
  if (filetype !== undefined && !lowlightCovers) warnIfHighlightSignalsTrouble(block);
  if (hasGrammar || lowlightCovers) {
    // A shipped grammar, or lowlight standing in for one, highlights this
    // block on its own: forcing a flat fg or drawUnstyledText would suppress
    // the per-token colors either produces. `markup.raw.block` is the code
    // style already registered for a plain block, so captures neither names
    // (most identifiers, punctuation, types) still read as code rather than
    // falling back to the prose default.
    if (lowlightCovers) block.onHighlight = highlightWithLowlight;
    block.baseHighlight = 'markup.raw.block';
    return;
  }
  block.fg = fg;
  // The default suppresses the plain-text draw while streaming and waits for
  // highlighting to supply styled chunks instead. With no grammar available
  // that wait resolves to plain text anyway, so the block would just be blank
  // until it did.
  block.drawUnstyledText = true;
}

/**
 * The fenced blocks somewhere under a renderable the renderer built.
 *
 * A fence is the only `CodeRenderable` the markdown renderer builds without a
 * chunk transform: prose and blockquote bodies are markdown text and go
 * through its link detector. Filetype does not separate the two, because a
 * fence may itself be labelled `markdown`.
 */
function* fencedBlocks(renderable: Renderable): Generator<CodeRenderable> {
  for (const child of renderable.getChildren()) {
    if (child instanceof CodeRenderable) {
      if (child.onChunks === undefined) yield child;
      continue;
    }
    yield* fencedBlocks(child);
  }
}

/**
 * Draws fenced code blocks on the theme's code surface.
 *
 * Inline code picks up `markup.raw` from the syntax style, but a fenced block
 * is rendered by `CodeRenderable`, which colors text from tree-sitter captures
 * for the block's own language. The package ships grammars for markdown,
 * JavaScript, TypeScript and Zig; the info string of a transcript fence
 * otherwise usually names a language it has no grammar for, though `lowlight`
 * (see `drawOnCodeSurface`) substitutes for a further set of those. Restyling
 * the default block gives it a code surface either way. When a grammar or a
 * `lowlight` language is available, per-token highlighting draws on top of
 * that surface. When neither is, the wait for highlighting would resolve to
 * plain text anyway, so the block is drawn flat and unstyled immediately
 * instead of sitting blank until it does.
 *
 * The default block is restyled rather than replaced. A replacement would
 * discard the margins, streaming mode, concealment, tree-sitter client, and
 * info-string normalization the renderer put on it, and the renderer would
 * stop tracking it: the visible symptom is a fenced block sitting flush
 * against the paragraph after it. `styles.test.ts` pins the margin, and a
 * wrapping box to give a short line's trailing cells the code surface was
 * tried and reverted for exactly that reason - the renderer computes a
 * block's bottom margin itself (`getInterBlockMargin`, plus a regex on the
 * raw token), so a wrapper would have to replicate that to keep the gap.
 *
 * A fence nested in a list is dispatched by `createListChildRenderable`, which
 * builds its `CodeRenderable` without consulting any override, so the list
 * branch restyles the whole subtree instead of waiting to be offered the
 * fence. Which of the two branches a nested fence goes through depends on the
 * block mode: `coalesced`, the transcript's, folds the list into a markdown
 * block, and the fence is then `markup.raw.block` inside it and never a
 * renderable of its own, so only `top-level` reaches the list branch. Both
 * routes end on the same surface, and `styles.test.ts` pins the mode that
 * decides which one runs.
 */
export function createMarkdownCodeRenderer(theme: Theme): MarkdownRenderNode {
  const surface = codeSurface(theme);
  return asCodeBlockOnly((token, context) => {
    if (token.type === 'code') {
      const block = context.defaultRender();
      if (!(block instanceof CodeRenderable)) return block;
      drawOnCodeSurface(block, surface);
      return block;
    }
    if (token.type !== 'list') return undefined;
    const list = context.defaultRender();
    if (list === null) return null;
    for (const block of fencedBlocks(list)) drawOnCodeSurface(block, surface);
    return list;
  });
}

/**
 * The options every transcript markdown block is built from.
 *
 * One definition because the code-block styling rests on what is in here and
 * on what is not. `internalBlockMode` is deliberately absent: its default,
 * `coalesced`, is what decides whether a fence nested in a list reaches
 * `createMarkdownCodeRenderer`'s list branch or is drawn from the syntax style
 * as part of a coalesced block.
 */
export function createMarkdownBlockOptions(
  theme: Theme,
  syntaxStyle: SyntaxStyle,
): MarkdownBlockOptions {
  return {
    syntaxStyle,
    conceal: true,
    tableOptions: createMarkdownTableOptions(theme),
    renderNode: createMarkdownCodeRenderer(theme),
    width: '100%',
  };
}

/**
 * A driver lifecycle line that reports a failure rather than a heartbeat.
 *
 * AgentShim drivers stream provider stderr and driver errors through the
 * diagnostic channel behind a `[<provider> error]` or `[<provider> stderr]`
 * marker, and nothing in the event distinguishes a crash from a turn
 * boundary. The marker is the only signal the transcript gets, so the failure
 * palette is chosen from it: `[codex error] ...` always, and a stderr line
 * whose own text opens with an error token.
 */
const FAILURE_DIAGNOSTIC =
  /^\[[^\s\]]+ error\]|^\[[^\s\]]+ stderr\] *(?:ERROR|FATAL|error:|fatal:|panic:|Traceback)/m;

export function conversationRole(entry: ConversationEntry): ConversationRole {
  if (entry.tone === 'failure') return 'failure';
  if (entry.tone === 'success') return 'success';
  if (entry.kind === 'assistant') return 'assistant';
  if (entry.kind === 'user') return 'user';
  if (entry.kind === 'prompt') return 'prompt';
  // A driver's own error markers are the only failure signal on this channel,
  // so they are promoted out of the muted narration style.
  if (entry.kind === 'diagnostic') {
    return FAILURE_DIAGNOSTIC.test(entry.content) ? 'failure' : 'analysis';
  }
  // An agent narrating its own work is analysis whichever channel carried it:
  // the diagnostic channel is where most backends put that narration, and
  // slate-on-slate buried it. Tool turns keep the neutral surface.
  if (entry.kind === 'analysis') return 'analysis';
  if (entry.kind === 'tool' || entry.kind === 'subprocess') return 'tool';
  return 'neutral';
}

export function entryPalette(entry: ConversationEntry, theme: Theme): EntryPalette {
  return theme.conversation[conversationRole(entry)];
}
