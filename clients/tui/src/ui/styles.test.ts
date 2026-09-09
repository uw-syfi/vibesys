import {afterEach, describe, expect, it} from 'bun:test';
import {
  type CapturedFrame,
  type CapturedSpan,
  CodeRenderable,
  type MarkdownOptions,
  MarkdownRenderable,
  type Renderable,
  rgbToHex,
  TextTableRenderable,
} from '@opentui/core';
import {createTestRenderer, MockTreeSitterClient} from '@opentui/core/testing';
import {
  createMarkdownBlockOptions,
  createMarkdownStyle,
  createMarkdownTableOptions,
} from './styles.js';
import {resolveTheme, THEME_NAMES, type Theme, type ThemeName} from './theme.js';

const cleanup: Array<() => void> = [];

afterEach(() => {
  for (const destroy of cleanup.splice(0).reverse()) destroy();
});

/**
 * Which node override a fixture gets: the transcript's own, none at all, or an
 * arbitrary one the test supplies.
 */
type CodeRenderer = 'transcript' | 'none' | NonNullable<MarkdownOptions['renderNode']>;

interface MarkdownFixtureOptions {
  codeRenderer?: CodeRenderer;
  /**
   * How the renderer splits blocks. The transcript leaves this unset and takes
   * the default; a fixture names it only to exercise the other mode's dispatch.
   */
  internalBlockMode?: MarkdownOptions['internalBlockMode'];
}

interface MarkdownFixture {
  markdown: MarkdownRenderable;
  treeSitterClient: MockTreeSitterClient;
  layout: () => Promise<void>;
  capture: () => CapturedFrame;
}

/**
 * A transcript markdown block, built from the options `ConversationView`
 * builds one from, so the assumptions these tests pin are the view's own.
 *
 * `'none'` drops only the code renderer, which makes the renderer's own output
 * the comparison point for what the override is allowed to change. Streaming
 * is on because that is the transcript's default outside a terminal, and it is
 * the mode whose defaults the override has to correct.
 */
async function renderMarkdown(
  content: string,
  theme: Theme,
  {codeRenderer = 'transcript', internalBlockMode}: MarkdownFixtureOptions = {},
): Promise<MarkdownFixture> {
  const {renderer, renderOnce, captureSpans} = await createTestRenderer({width: 60, height: 20});
  cleanup.push(() => renderer.destroy());
  const treeSitterClient = new MockTreeSitterClient();
  cleanup.push(() => void treeSitterClient.destroy());
  const {renderNode: transcriptRenderNode, ...blockOptions} = createMarkdownBlockOptions(
    theme,
    createMarkdownStyle(theme),
  );
  const renderNode =
    codeRenderer === 'transcript'
      ? transcriptRenderNode
      : codeRenderer === 'none'
        ? undefined
        : codeRenderer;
  const markdown = new MarkdownRenderable(renderer, {
    ...blockOptions,
    ...(renderNode === undefined ? {} : {renderNode}),
    ...(internalBlockMode === undefined ? {} : {internalBlockMode}),
    content,
    streaming: true,
    treeSitterClient,
  });
  renderer.root.add(markdown);
  cleanup.push(() => markdown.destroyRecursively());
  return {markdown, treeSitterClient, layout: renderOnce, capture: captureSpans};
}

/**
 * The colors the drawn cells carrying `text` ended up with.
 *
 * Read off the frame rather than off a renderable because a fence nested in a
 * list is not a renderable of its own: it is a run of cells inside the block
 * its list was coalesced into.
 */
function drawnSurface(fixture: MarkdownFixture, text: string): {fg: string; bg: string} {
  const spans = fixture
    .capture()
    .lines.flatMap(line => line.spans)
    .filter(span => span.text.includes(text));
  expect(spans).toHaveLength(1);
  const span = spans[0] as CapturedSpan;
  return {fg: rgbToHex(span.fg), bg: rgbToHex(span.bg)};
}

/**
 * The block for one fence, wherever in the tree the renderer put it.
 *
 * Prose blocks are `CodeRenderable`s too (the renderer highlights them as
 * markdown), so they are told apart by content: a prose block carries the
 * fence markers, the fenced block only its body. Finding exactly one is itself
 * the claim that the fence became a renderable of its own.
 */
function fencedBlock(markdown: MarkdownRenderable, body: string): CodeRenderable {
  const blocks = codeBlocks(markdown).filter(block => block.content === body);
  expect(blocks).toHaveLength(1);
  return blocks[0] as CodeRenderable;
}

/** Every `CodeRenderable` anywhere under a rendered markdown block, in draw order. */
function codeBlocks(renderable: Renderable): CodeRenderable[] {
  const blocks: CodeRenderable[] = [];
  for (const child of renderable.getChildren()) {
    if (child instanceof CodeRenderable) blocks.push(child);
    else blocks.push(...codeBlocks(child));
  }
  return blocks;
}

/** Blank rows between the fenced block and the block after it. */
function gapAfterFence({markdown}: MarkdownFixture, body: string): number {
  const blocks = markdown.getChildren();
  const fence = fencedBlock(markdown, body);
  const next = blocks[blocks.indexOf(fence) + 1];
  expect(next).toBeDefined();
  return (next as (typeof blocks)[number]).y - (fence.y + fence.height);
}

/** The surface a fence draws on, whichever renderer path produced it. */
function codeSurface(theme: Theme): {fg: string; bg: string} {
  return {fg: theme.markdown.code, bg: theme.markdown.codeBackground};
}

/**
 * The reviewer's input: a list item followed by an indented fence naming a
 * grammar that does not exist, an unlabelled one, and a top-level fence to
 * compare them against.
 */
const NESTED_FENCES = [
  '- A list item.',
  '',
  '  ```mystery',
  '  nested body',
  '  ```',
  '',
  '- An unlabelled item.',
  '',
  '  ```',
  '  unlabelled body',
  '  ```',
  '',
  '```mystery',
  'top level body',
  '```',
  '',
].join('\n');

describe('markdown table options', () => {
  it('sizes columns to content rather than to the pane', () => {
    // The renderer's own default expands columns to the available width, which
    // pads a three-character cell across a quarter of the transcript.
    const options = createMarkdownTableOptions(resolveTheme('dark'));
    expect(options.widthMode).toBe('content');
    expect(options.wrapMode).toBe('word');
  });

  it.each([...THEME_NAMES])('draws %s borders in the theme, not a fixed grey', name => {
    const theme = resolveTheme(name);
    const options = createMarkdownTableOptions(theme);
    expect(options.borderColor).toBe(theme.border);
    // The regression this guards: the renderer defaults to #888888 for table
    // borders regardless of theme, which is invisible on a light canvas.
    expect(options.borderColor).not.toBe('#888888');
  });
});

describe('markdown code blocks', () => {
  it.each<ThemeName>([
    'dark',
    'light',
  ])('draws a fenced block on the %s code surface', async name => {
    const theme = resolveTheme(name);
    const {markdown} = await renderMarkdown('```rust\nlet x = 1;\n```\n', theme);

    // Without this the block inherits the card's colors and reads as prose:
    // no grammars ship with the renderer, so highlighting is not available to
    // supply them.
    const code = fencedBlock(markdown, 'let x = 1;');
    expect(rgbToHex(code.fg)).toBe(theme.markdown.code);
    expect(rgbToHex(code.bg)).toBe(theme.markdown.codeBackground);
  });

  it('styles a fenced block that declares no language', async () => {
    const theme = resolveTheme('light');
    const {markdown} = await renderMarkdown('```\nplain text\n```\n', theme);

    const code = fencedBlock(markdown, 'plain text');
    expect(rgbToHex(code.fg)).toBe(theme.markdown.code);
    expect(rgbToHex(code.bg)).toBe(theme.markdown.codeBackground);
  });

  it('draws code text without waiting for a grammar that never arrives', async () => {
    const theme = resolveTheme('dark');
    const fence = '```rust\nlet x = 1;\n```\n';
    const {markdown} = await renderMarkdown(fence, theme);
    const {markdown: plain} = await renderMarkdown(fence, theme, {codeRenderer: 'none'});

    // Streaming suppresses the plain-text draw until highlighting supplies
    // styled chunks. Nothing ever does here, so the block would stay blank.
    expect(fencedBlock(plain, 'let x = 1;').drawUnstyledText).toBe(false);
    expect(fencedBlock(markdown, 'let x = 1;').drawUnstyledText).toBe(true);
  });

  it('keeps the margin the renderer puts after a fenced block', async () => {
    const theme = resolveTheme('dark');
    const content = '```rust\nlet x = 1;\n```\n\nProse after the block.\n';
    const styled = await renderMarkdown(content, theme);
    const plain = await renderMarkdown(content, theme, {codeRenderer: 'none'});
    await styled.layout();
    await plain.layout();

    // Replacing the block instead of restyling it drops it out of the
    // renderer's margin tracking, and it ends up flush against the paragraph.
    // `marginBottom` is write-only, so the gap is read off the laid-out rows.
    expect(gapAfterFence(plain, 'let x = 1;')).toBe(1);
    expect(gapAfterFence(styled, 'let x = 1;')).toBe(1);
  });

  it('styles the block the renderer built rather than a replacement', async () => {
    const theme = resolveTheme('dark');
    const {markdown, treeSitterClient} = await renderMarkdown('```rs\nlet x = 1;\n```\n', theme);

    const code = fencedBlock(markdown, 'let x = 1;');
    // A freshly constructed block would fall back to the process-wide client
    // and would take the info string verbatim instead of normalizing it.
    expect(code.treeSitterClient).toBe(treeSitterClient);
    expect(code.filetype).toBe('rust');
    expect(code.streaming).toBe(true);
  });

  it('draws more than one colour for a fence whose grammar ships', async () => {
    const theme = resolveTheme('dark');
    const content = 'const x = 1;';
    const fixture = await renderMarkdown(`\`\`\`ts\n${content}\n\`\`\`\n`, theme);
    await fixture.layout();

    const code = fencedBlock(fixture.markdown, content);
    expect(code.filetype).toBe('typescript');
    // The mock client never runs a real parser; feeding it a highlight result
    // stands in for a grammar resolving, which is the case this override must
    // let through undisturbed instead of overwriting with a flat color.
    expect(code.baseHighlight).toBe('markup.raw.block');
    expect(code.drawUnstyledText).toBe(false);

    fixture.treeSitterClient.setMockResult({
      highlights: [
        [0, 5, 'keyword'],
        [10, 11, 'number'],
      ],
    });
    fixture.treeSitterClient.resolveAllHighlightOnce();
    await code.highlightingDone;
    await fixture.layout();

    expect(rgbToHex(code.bg)).toBe(theme.markdown.codeBackground);
    expect(drawnSurface(fixture, 'const').fg).toBe(theme.markdown.keyword);
    expect(drawnSurface(fixture, '1').fg).toBe(theme.markdown.number);
    // Uncaptured text ("x", "=") still reads as code rather than the markdown
    // default: `baseHighlight` carries the code surface's fg into every span
    // the two captures above do not own.
    expect(drawnSurface(fixture, ' x = ').fg).toBe(theme.markdown.code);
  });

  it('leaves prose coalesced instead of one renderable per paragraph', async () => {
    const theme = resolveTheme('dark');
    const content = Array.from({length: 100}, (_, index) => `Paragraph ${index + 1}.`).join('\n\n');
    const {markdown} = await renderMarkdown(content, theme);
    const {markdown: plain} = await renderMarkdown(content, theme, {codeRenderer: 'none'});
    // An override the renderer cannot rule out for ordinary tokens is what
    // costs the coalescing, whatever that override ends up returning.
    const {markdown: unscoped} = await renderMarkdown(content, theme, {
      codeRenderer: (_token, context) => context.defaultRender(),
    });

    expect(plain.getChildren()).toHaveLength(1);
    expect(markdown.getChildren()).toHaveLength(plain.getChildren().length);
    expect(unscoped.getChildren()).toHaveLength(100);
  });

  it('draws fences nested in a list on the same surface as a top-level one', async () => {
    const theme = resolveTheme('dark');
    const fixture = await renderMarkdown(NESTED_FENCES, theme);
    await fixture.layout();

    // Nothing resolves a grammar here, which is the case the override exists
    // for. Both nested fences still have to draw, and on the same surface the
    // top-level fence gets.
    expect(drawnSurface(fixture, 'nested body')).toEqual(codeSurface(theme));
    expect(drawnSurface(fixture, 'unlabelled body')).toEqual(codeSurface(theme));
    expect(drawnSurface(fixture, 'top level body')).toEqual(codeSurface(theme));
    const prose = drawnSurface(fixture, 'A list item.');
    expect(prose.fg).toBe(theme.markdown.default);
    expect(prose.bg).not.toBe(theme.markdown.codeBackground);
  });

  it('keeps the transcript in the block mode that routes nested fences away from list children', async () => {
    const theme = resolveTheme('dark');
    const fixture = await renderMarkdown(NESTED_FENCES, theme);
    await fixture.layout();

    // The fact the surface test above rests on, asserted rather than assumed.
    // `createListChildRenderable` builds a nested fence's `CodeRenderable`
    // straight from the `code` token without consulting `renderNode`, and
    // "coalesced" is the only reason the transcript never reaches it: it folds
    // a list into the surrounding markdown block, where the fence is
    // `markup.raw.block` inside a block and not a renderable of its own. Only
    // the top-level fence is a second block. If this fails, the list-child
    // path is live for the transcript, and the test below is the one that says
    // the fences are still styled there.
    expect(fixture.markdown.internalBlockMode).toBe('coalesced');
    expect(fixture.markdown.getChildren()).toHaveLength(2);
    expect(codeBlocks(fixture.markdown).map(block => block.content)).not.toContain('nested body');
  });

  it('styles fences the list-child path builds, which another block mode would reach', async () => {
    const theme = resolveTheme('dark');
    const fixture = await renderMarkdown(NESTED_FENCES, theme, {internalBlockMode: 'top-level'});
    const plain = await renderMarkdown(NESTED_FENCES, theme, {
      codeRenderer: 'none',
      internalBlockMode: 'top-level',
    });
    await fixture.layout();
    await plain.layout();

    // "top-level" keeps the list a block of its own, so each nested fence is
    // now a `CodeRenderable` that `createListChildRenderable` built. Finding
    // one per body is what proves this ran the nested dispatch rather than the
    // coalesced default again.
    for (const body of ['nested body', 'unlabelled body', 'top level body']) {
      const block = fencedBlock(fixture.markdown, body);
      expect({fg: rgbToHex(block.fg), bg: rgbToHex(block.bg)}).toEqual(codeSurface(theme));
      expect(block.drawUnstyledText).toBe(true);
      // What the renderer alone does with the same block: no code surface, and
      // nothing drawn while it waits on highlighting that never arrives.
      const bare = fencedBlock(plain.markdown, body);
      expect({fg: rgbToHex(bare.fg), bg: rgbToHex(bare.bg)}).not.toEqual(codeSurface(theme));
      expect(bare.drawUnstyledText).toBe(false);
    }
    expect(drawnSurface(fixture, 'nested body')).toEqual(codeSurface(theme));
    expect(drawnSurface(fixture, 'unlabelled body')).toEqual(codeSurface(theme));
    // The list item's own prose is a `CodeRenderable` here too, and restyling
    // the subtree indiscriminately would put it on the code surface as well.
    expect(drawnSurface(fixture, 'A list item.').bg).not.toBe(theme.markdown.codeBackground);
  });

  it('leaves every other block to the default renderer', async () => {
    const theme = resolveTheme('dark');
    const content = '# Heading\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n> quote\n\n- item\n';
    const {markdown} = await renderMarkdown(content, theme);
    const {markdown: plain} = await renderMarkdown(content, theme, {codeRenderer: 'none'});

    const surfaces = (block: MarkdownRenderable): unknown[] =>
      block.getChildren().map(child => ({
        kind: child.constructor.name,
        ...(child instanceof CodeRenderable
          ? {fg: rgbToHex(child.fg), bg: rgbToHex(child.bg)}
          : {}),
      }));
    expect(surfaces(markdown)).toEqual(surfaces(plain));
    expect(markdown.getChildren().some(child => child instanceof TextTableRenderable)).toBe(true);
  });
});
