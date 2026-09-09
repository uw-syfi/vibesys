import {afterEach, describe, expect, it} from 'bun:test';
import {BoxRenderable} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {SessionController} from '../session-controller.js';
import {type ConversationEntry, initialSessionState} from '../session-model.js';
import {ConversationView} from './conversation.js';
import {createMarkdownStyle} from './styles.js';
import {
  CONVERSATION_ROLES,
  contrastRatio,
  ensureContrast,
  listThemes,
  resolveTheme,
  SUBTLE_TEXT_MIN_CONTRAST,
} from './theme.js';

/**
 * #565: an entry separates from its neighbour with a rule on its top edge,
 * not a four-sided bordered card sitting under its own blank margin row.
 * These render through the real OpenTUI test renderer rather than computing
 * expected row counts by hand: a pure-function test would not catch a stray
 * margin or an extra border side reappearing, which is exactly the class of
 * bug this issue is about (docs/contributing/coding-best-practices.md).
 */
describe('conversation entry row cost (#565)', () => {
  const cleanup: Array<() => void> = [];
  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
  });

  const controller = {} as unknown as SessionController;

  async function renderEntries(
    entries: ConversationEntry[],
  ): Promise<{testRenderer: TestRendererSetup; view: ConversationView}> {
    const theme = resolveTheme(null);
    const testRenderer = await createTestRenderer({width: 80, height: 40});
    const view = new ConversationView(
      testRenderer.renderer,
      controller,
      createMarkdownStyle(theme),
      theme,
      // Entry selection from state is a separate, already-tested concern;
      // fixing the list here keeps this test about row cost alone.
      {selectConversation: () => entries},
    );
    testRenderer.renderer.root.add(view.output);
    cleanup.push(() => {
      view.output.destroyRecursively();
      testRenderer.renderer.destroy();
    });
    view.render(initialSessionState());
    await testRenderer.renderOnce();
    return {testRenderer, view};
  }

  // 'status', 'analysis' and 'result' all skip both the markdown pipeline and
  // the tool-output preview (see the branches in `#renderEntry`), so each
  // renders its content verbatim on exactly one line. That isolates the row
  // count this test pins from #516's separate, still-open content-preview
  // question.
  const oneLineEntries: ConversationEntry[] = [
    {id: 'a', kind: 'status', label: 'status', content: 'one line'},
    {id: 'b', kind: 'analysis', label: 'analysis', content: 'one line'},
    {id: 'c', kind: 'result', label: 'result', content: 'one line'},
  ];

  it('costs a divider and a heading per entry, not a margin row plus a four-sided card', async () => {
    const {testRenderer, view} = await renderEntries(oneLineEntries);
    for (const entry of oneLineEntries) {
      const card = view.output.findDescendantById(`event-${entry.id}`);
      if (!(card instanceof BoxRenderable))
        throw new Error(`entry ${entry.id} did not render a card`);
      // Divider (1) + heading (1) + one content line (1). A bordered card
      // with its own margin row cost 5 for the same content; even the
      // pre-#565 borderless 'status' case still cost 3 rows for a margin
      // and a heading before a single line of content ever appeared. Every
      // kind now costs the same, and this one has no margin to add.
      expect(card.height).toBe(3);
      expect(card.border).toEqual(['top']);
    }
    // No stray rows between cards either: three one-line entries cost
    // exactly 9 rows end to end. The pre-#565 card cost 5 rows each (15
    // total); this assertion fails at that revision and passes at this one.
    expect(view.output.height).toBe(9);
    void testRenderer;
  });

  it('does not add its own inset on top of the pane that contains it', async () => {
    const leadingColumn = async (entries: ConversationEntry[]): Promise<number> => {
      const {testRenderer} = await renderEntries(entries);
      const frame = testRenderer.captureCharFrame();
      const row = frame.split('\n').find(line => line.trim().length > 0);
      if (row === undefined) throw new Error('nothing rendered');
      return row.length - row.trimStart().length;
    };
    const emptyColumn = await leadingColumn([]);
    const entryColumn = await leadingColumn([
      {id: 'a', kind: 'status', label: 'status', content: 'x'},
    ]);
    // Both the empty-transcript message and an entry's heading are direct
    // children of the same unpadded `output` box: a card that added its own
    // left padding or border, as it did before #565, would start one or
    // more columns later than the empty-transcript message sitting beside
    // it in the same pane.
    expect(entryColumn).toBe(emptyColumn);
  });
});

/**
 * Role and selection have to stay distinguishable in all eight themes
 * (#565's fourth acceptance criterion). theme.ts's colours come out of
 * `mix()` and `ensureContrast()` and exist nowhere as literals, so this
 * evaluates the theme module directly rather than transcribing hexes.
 *
 * This is a pure computation over theme tokens, not a rendered frame: per
 * docs/contributing/tui-architecture.md, contrast and legibility are pinned
 * at that layer.
 */
describe('role and selection stay distinguishable across every theme (#565)', () => {
  it('holds every role divider to the 3:1 floor textSubtle uses for punctuation and rules', () => {
    for (const theme of listThemes()) {
      const restingColors = CONVERSATION_ROLES.map(role => {
        // Mirrors the `ensureContrast` call `#renderEntry` makes on
        // `palette.border` before using it as the resting divider colour.
        const resting = ensureContrast(
          theme.conversation[role].border,
          theme.canvas,
          SUBTLE_TEXT_MIN_CONTRAST,
        );
        expect(contrastRatio(resting, theme.canvas)).toBeGreaterThanOrEqual(
          SUBTLE_TEXT_MIN_CONTRAST,
        );
        return resting;
      });
      // Nudging a marginal accent toward black or white must not collapse
      // two roles onto the same divider colour.
      expect(new Set(restingColors).size).toBe(restingColors.length);
    }
  });

  it('keeps the selection colour itself legible in every theme', () => {
    for (const theme of listThemes()) {
      expect(contrastRatio(theme.borderFocus, theme.canvas)).toBeGreaterThanOrEqual(
        SUBTLE_TEXT_MIN_CONTRAST,
      );
    }
  });
});
