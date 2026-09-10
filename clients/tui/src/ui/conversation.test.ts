import {afterEach, describe, expect, it} from 'bun:test';
import {BoxRenderable, type Renderable} from '@opentui/core';
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

const cleanup: Array<() => void> = [];
afterEach(() => {
  for (const destroy of cleanup.splice(0).reverse()) destroy();
});

const controller = {} as unknown as SessionController;

interface Mounted {
  testRenderer: TestRendererSetup;
  view: ConversationView;
  /** Draws a list into the same view, so incremental paths are exercised. */
  draw(entries: ConversationEntry[], selectedId?: string | null): Promise<void>;
}

async function mount(): Promise<Mounted> {
  const theme = resolveTheme(null);
  const testRenderer = await createTestRenderer({width: 80, height: 40});
  // The list is swapped per draw rather than fixed at construction, because
  // the incremental paths below need one view to see several lists.
  let entries: ConversationEntry[] = [];
  const view = new ConversationView(
    testRenderer.renderer,
    controller,
    createMarkdownStyle(theme),
    theme,
    {selectConversation: () => entries, showsSelection: true},
  );
  testRenderer.renderer.root.add(view.output);
  cleanup.push(() => {
    view.output.destroyRecursively();
    testRenderer.renderer.destroy();
  });
  return {
    testRenderer,
    view,
    draw: async (next, selectedId = null) => {
      entries = next;
      view.render({...initialSessionState(), selectedEntryId: selectedId});
      await testRenderer.renderOnce();
    },
  };
}

async function renderEntries(
  entries: ConversationEntry[],
  selectedId: string | null = null,
): Promise<Mounted> {
  const mounted = await mount();
  await mounted.draw(entries, selectedId);
  return mounted;
}

function cardOf(view: ConversationView, id: string): BoxRenderable {
  const card = view.output.findDescendantById(`event-${id}`);
  if (!(card instanceof BoxRenderable)) throw new Error(`entry ${id} did not render a card`);
  return card;
}

/** An entry from a named agent in a named round: the pair a heading splits. */
function from(
  who: {agentKind: string; roundLabel: string},
  id: string,
  content: string,
): ConversationEntry {
  return {id, kind: 'status', label: `${who.agentKind} · ${who.roundLabel}`, ...who, content};
}

const judge = {agentKind: 'judge', roundLabel: 'round-1-retry-1-judge'};
const implementer = {agentKind: 'implementer', roundLabel: 'round-1-implementer'};
const judgeAgain = {agentKind: 'judge', roundLabel: 'round-2-judge'};

/**
 * The tree a view built, minus the auto-generated ids that differ between two
 * renderers. Chrome is structure, so an incremental path that gets it wrong
 * lands here as a card with the wrong height, border, or children.
 */
function shapeOf(view: ConversationView): string {
  const describeNode = (node: Renderable, depth: number): string[] => [
    `${'  '.repeat(depth)}${node.id.startsWith('event-') ? node.id : node.constructor.name}` +
      ` x=${node.x} y=${node.y} w=${node.width} h=${node.height}` +
      (node instanceof BoxRenderable ? ` border=${JSON.stringify(node.border)}` : ''),
    ...node.getChildren().flatMap(child => describeNode(child, depth + 1)),
  ];
  return describeNode(view.output, 0).join('\n');
}

/**
 * #565: an entry separates from its neighbour with a rule on its top edge,
 * not a four-sided bordered card sitting under its own blank margin row.
 * These render through the real OpenTUI test renderer rather than computing
 * expected row counts by hand: a pure-function test would not catch a stray
 * margin or an extra border side reappearing, which is exactly the class of
 * bug this issue is about (docs/contributing/coding-best-practices.md).
 */
describe('conversation entry row cost (#565)', () => {
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

  it('pins the role to the left edge and the run id to the right', async () => {
    // The role is what an operator scans down the column, so it holds the left
    // edge; the run id is per-entry detail and would otherwise push the eye a
    // variable distance rightward on every line. An entry with no agent/round
    // pair keeps its single label on the left.
    const entries: ConversationEntry[] = [
      {
        id: 'split',
        kind: 'analysis',
        label: 'judge · round-1-retry-1-judge',
        agentKind: 'judge',
        roundLabel: 'round-1-retry-1-judge',
        content: 'one line',
      },
      {id: 'plain', kind: 'result', label: 'round-1 · PASS', content: 'one line'},
    ];
    const {view} = await renderEntries(entries);

    const split = view.output.findDescendantById('event-split-heading');
    if (!(split instanceof BoxRenderable)) throw new Error('split heading missing');
    const [role, runId] = split.getChildren();
    if (role === undefined || runId === undefined)
      throw new Error('split heading did not render two parts');
    expect(role.x).toBe(split.x);
    expect(runId.x + runId.width).toBe(split.x + split.width);
    // Not merely offset: the run id must actually sit to the right of the role.
    expect(runId.x).toBeGreaterThan(role.x + role.width);

    // An entry without the pair is untouched, one child on the left edge.
    const plain = view.output.findDescendantById('event-plain-heading');
    if (!(plain instanceof BoxRenderable)) throw new Error('plain heading missing');
    expect(plain.getChildren()).toHaveLength(1);
    expect(plain.getChildren()[0]?.x).toBe(plain.x);
  });

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
    const empty = await renderEntries([]);
    const message = empty.view.output.getChildren()[0];
    const {view} = await renderEntries([{id: 'a', kind: 'status', label: 'status', content: 'x'}]);
    const heading = view.output.findDescendantById('event-a-heading');
    if (!(heading instanceof BoxRenderable)) throw new Error('heading missing');
    const [role] = heading.getChildren();
    if (message === undefined || role === undefined) throw new Error('nothing rendered');
    // A card adds no inset of its own beyond the one column every entry
    // reserves for the selection rule, and the empty-transcript message takes
    // that same column: before #565 a card padded itself on top of the pane's
    // inset and started several columns right of the message sitting beside it
    // in the same pane, and a gutter only some of the children honoured would
    // reopen that gap by one column.
    expect(role.x).toBe(message.x);
    expect(role.x).toBe(view.output.x + 1);
  });

  it('spends one divider and one heading on a run of entries from one speaker', async () => {
    const run = ['a', 'b', 'c', 'd', 'e', 'f'].map(id => from(judge, id, `line ${id}`));
    const {view} = await renderEntries(run);
    // 2 + N, not 3N. Six one-line judge entries cost 8 rows; they cost 18
    // before this change, restating `judge` and `round-1-retry-1-judge` six
    // times each for no added information.
    expect(view.output.height).toBe(2 + run.length);
    const [first, ...rest] = run.map(entry => cardOf(view, entry.id));
    expect(first?.height).toBe(3);
    expect(first?.border).toEqual(['top']);
    for (const card of rest) {
      expect(card.height).toBe(1);
      // `false`, not an empty side list: OpenTUI turns a border back on if a
      // style or colour is passed beside it (tui-conventions.md).
      expect(card.border).toBe(false);
    }
    for (const entry of run.slice(1))
      expect(view.output.findDescendantById(`event-${entry.id}-heading`)).toBeUndefined();
  });

  it('redraws the chrome when the speaker changes', async () => {
    const entries = [
      from(judge, 'j1', 'one'),
      from(judge, 'j2', 'two'),
      from(implementer, 'i1', 'three'),
      from(judgeAgain, 'j3', 'four'),
      // No agent/round pair, so the speaker is the label: two in a row are one
      // run, and the differing label after them opens another.
      {id: 's1', kind: 'status' as const, label: 'launcher', content: 'five'},
      {id: 's2', kind: 'status' as const, label: 'launcher', content: 'six'},
      {id: 's3', kind: 'status' as const, label: 'backend', content: 'seven'},
    ];
    const {view} = await renderEntries(entries);
    const opensRun = (id: string): boolean => cardOf(view, id).height === 3;
    expect(opensRun('j1')).toBe(true);
    expect(opensRun('j2')).toBe(false);
    expect(opensRun('i1')).toBe(true);
    // The same agent in a different round is a different speaker: the run key
    // is the pair the heading splits on, not the role alone.
    expect(opensRun('j3')).toBe(true);
    expect(opensRun('s1')).toBe(true);
    expect(opensRun('s2')).toBe(false);
    expect(opensRun('s3')).toBe(true);
    expect(view.output.height).toBe(3 + 1 + 3 + 3 + 3 + 1 + 3);
  });

  it('shows the cursor on an entry inside a run without moving its content', async () => {
    const run = [from(judge, 'a', 'one'), from(judge, 'b', 'two'), from(judge, 'c', 'three')];
    const resting = await renderEntries(run);
    const selected = await renderEntries(run, 'b');
    const card = cardOf(selected.view, 'b');
    // An entry inside a run draws no heading, so it has no marker cell and no
    // `textStrong` label to carry the cursor. A rule down its left edge is a
    // channel every entry has, and it is a glyph rather than a colour, which
    // is what WCAG 1.4.1 asks for (tui-conventions.md).
    expect(card.border).toEqual(['left']);
    expect(selected.testRenderer.captureCharFrame()).toContain('│two');
    // It costs no row and shifts no text: the column it draws into is the one
    // an unselected card reserves with padding, so moving the cursor through a
    // run leaves the transcript exactly where it was.
    expect(card.height).toBe(cardOf(resting.view, 'b').height);
    expect(selected.view.output.height).toBe(resting.view.output.height);
    expect(card.getChildren()[0]?.x).toBe(cardOf(resting.view, 'b').getChildren()[0]?.x);
    // The entry that opens the run keeps both rules and stays put too.
    const head = cardOf(await renderEntries(run, 'a').then(mounted => mounted.view), 'a');
    expect(head.border).toEqual(['top', 'left']);
    expect(head.getChildren()[0]?.x).toBe(cardOf(resting.view, 'a').getChildren()[0]?.x);
  });
});

/**
 * Chrome now depends on the entry above, and the view renders incrementally:
 * it appends a tail, prepends revealed history, and replaces one changed card
 * in place, without rebuilding (`CONVERSATION_WINDOW_THRESHOLD` exists so it
 * never has to). Each of those can leave a neighbour holding chrome it should
 * have lost, or missing chrome it should have gained, so each is pinned
 * against a fresh render of the same list.
 */
describe('speaker runs survive incremental rendering (#565)', () => {
  it('appends without redrawing what is already on screen', async () => {
    const shown = [from(judge, 'a', 'one'), from(judge, 'b', 'two')];
    const grown = [...shown, from(judge, 'c', 'three'), from(implementer, 'i', 'four')];
    const mounted = await mount();
    await mounted.draw(shown);
    const [head, second] = mounted.view.output.getChildren();
    await mounted.draw(grown);

    // Incremental, not a rebuild: the cards already on screen are the same
    // renderables, and only the new tail was built.
    expect(mounted.view.output.getChildren()[0]).toBe(head);
    expect(mounted.view.output.getChildren()[1]).toBe(second);
    // The appended judge entry continues the run; the implementer opens one.
    expect(cardOf(mounted.view, 'c').height).toBe(1);
    expect(cardOf(mounted.view, 'i').height).toBe(3);
    const fresh = await renderEntries(grown);
    expect(shapeOf(mounted.view)).toBe(shapeOf(fresh.view));
    expect(mounted.testRenderer.captureCharFrame()).toBe(fresh.testRenderer.captureCharFrame());
  });

  it('takes the heading off the entry that a prepend pushes out of first place', async () => {
    const tail = [from(judge, 'b', 'two'), from(judge, 'c', 'three')];
    const full = [from(judge, 'a', 'one'), ...tail];
    const mounted = await mount();
    await mounted.draw(tail);
    // 'b' opened the view, so it drew a heading for being first.
    expect(cardOf(mounted.view, 'b').height).toBe(3);
    const kept = mounted.view.output.getChildren()[1];
    await mounted.draw(full);

    // It is not first any more and the entry revealed above it is the same
    // speaker, so its chrome goes.
    expect(cardOf(mounted.view, 'b').height).toBe(1);
    expect(cardOf(mounted.view, 'a').height).toBe(3);
    // Only that one neighbour was redrawn; the rest of the tail is untouched.
    expect(mounted.view.output.getChildren()[2]).toBe(kept);
    const fresh = await renderEntries(full);
    expect(shapeOf(mounted.view)).toBe(shapeOf(fresh.view));
    expect(mounted.testRenderer.captureCharFrame()).toBe(fresh.testRenderer.captureCharFrame());
  });

  it('redraws the neighbour whose chrome a replaced entry decides', async () => {
    const head = from(judge, 'a', 'one');
    const last = from(judge, 'c', 'three');
    const asJudge = [head, from(judge, 'b', 'two'), last];
    // The same entry re-emitted, same id, different speaker: it breaks the run
    // it was part of, so 'c' below it has to gain the chrome it did not draw.
    const asImplementer = [head, from(implementer, 'b', 'two'), last];
    const mounted = await mount();
    await mounted.draw(asJudge);
    expect(cardOf(mounted.view, 'c').height).toBe(1);

    await mounted.draw(asImplementer);
    expect(cardOf(mounted.view, 'b').height).toBe(3);
    expect(cardOf(mounted.view, 'c').height).toBe(3);
    const changed = await renderEntries(asImplementer);
    expect(shapeOf(mounted.view)).toBe(shapeOf(changed.view));
    expect(mounted.testRenderer.captureCharFrame()).toBe(changed.testRenderer.captureCharFrame());

    // And back: the neighbour loses the chrome again when the run re-forms.
    await mounted.draw(asJudge);
    expect(cardOf(mounted.view, 'c').height).toBe(1);
    const restored = await renderEntries(asJudge);
    expect(shapeOf(mounted.view)).toBe(shapeOf(restored.view));
    expect(mounted.testRenderer.captureCharFrame()).toBe(restored.testRenderer.captureCharFrame());
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
