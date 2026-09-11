import {afterEach, describe, expect, it} from 'bun:test';
import {BoxRenderable, type Renderable, rgbToHex, TextRenderable} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {SessionController} from '../session-controller.js';
import {type ConversationEntry, initialSessionState} from '../session-model.js';
import {ConversationView, styleSourceTags} from './conversation.js';
import {createMarkdownStyle} from './styles.js';
import {
  CONVERSATION_ROLES,
  contrastRatio,
  ensureContrast,
  listThemes,
  resolveTheme,
  SUBTLE_TEXT_MIN_CONTRAST,
  type Theme,
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

async function mount(theme: Theme = resolveTheme(null)): Promise<Mounted> {
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
  theme?: Theme,
): Promise<Mounted> {
  const mounted = await mount(theme);
  await mounted.draw(entries, selectedId);
  return mounted;
}

function cardOf(view: ConversationView, id: string): BoxRenderable {
  const card = view.output.findDescendantById(`event-${id}`);
  if (!(card instanceof BoxRenderable)) throw new Error(`entry ${id} did not render a card`);
  return card;
}

/**
 * An entry from a named agent in a named round: the pair a heading splits.
 *
 * 'analysis' because run collapsing is about entries drawn as cards. #620's
 * bare kinds ('status', and unflagged 'diagnostic'/'subprocess') keep their own
 * grammar and are covered separately below.
 */
function from(
  who: {agentKind: string; roundLabel: string},
  id: string,
  content: string,
): ConversationEntry {
  return {id, kind: 'analysis', label: `${who.agentKind} · ${who.roundLabel}`, ...who, content};
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
    // Divider (1) + heading (1) + one content line (1) for an entry drawn as a
    // card. A bordered card with its own margin row cost 5 rows for the same
    // content.
    //
    // 'status' is the one kind that does not draw the divider, and that is not
    // this change's doing: #620 demoted lifecycle chatter to bare lines, so a
    // status entry draws no frame at all and costs a heading plus its content.
    // Giving it the rule would undo that demotion, so the row cost is asserted
    // per kind rather than as one number for all of them.
    const expected: Record<string, {height: number; border: boolean | 'top'[]}> = {
      a: {height: 2, border: false},
      b: {height: 3, border: ['top']},
      c: {height: 3, border: ['top']},
    };
    const {testRenderer, view} = await renderEntries(oneLineEntries);
    for (const entry of oneLineEntries) {
      const card = view.output.findDescendantById(`event-${entry.id}`);
      if (!(card instanceof BoxRenderable))
        throw new Error(`entry ${entry.id} did not render a card`);
      const want = expected[entry.id];
      if (want === undefined) throw new Error(`no expectation for ${entry.id}`);
      expect([entry.id, card.height]).toEqual([entry.id, want.height]);
      expect([entry.id, card.border]).toEqual([entry.id, want.border]);
    }
    // No stray rows between cards either: the three entries cost exactly 9 rows
    // end to end (2 + 3 + 3, plus the single margin row the bare status entry
    // keeps from #620). The pre-#565 card cost 5 rows each, 15 total.
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
      {id: 's1', kind: 'result' as const, label: 'launcher', content: 'five'},
      {id: 's2', kind: 'result' as const, label: 'launcher', content: 'six'},
      {id: 's3', kind: 'result' as const, label: 'backend', content: 'seven'},
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

  it('collapses a run of #620 bare entries without giving one a divider', async () => {
    // Bare lifecycle lines are most of what run collapsing buys: a run of them
    // is one agent talking, and it restated the agent above every line. They
    // lose the repeated heading like any other entry and never gain the
    // divider, which is the half of the chrome #620's demotion is about.
    const entries: ConversationEntry[] = [
      {id: 'b1', kind: 'status', label: 'launcher', content: 'one'},
      {id: 'b2', kind: 'status', label: 'launcher', content: 'two'},
      {id: 'b3', kind: 'status', label: 'launcher', content: 'three'},
      from(judge, 'j1', 'four'),
      from(judge, 'j2', 'five'),
    ];
    const {view} = await renderEntries(entries);
    for (const id of ['b1', 'b2', 'b3']) expect([id, cardOf(view, id).border]).toEqual([id, false]);
    // The opener keeps heading and content; the rest of the run is content
    // alone, and #620's margin row goes with the heading rather than splitting
    // the run with a blank line.
    expect(cardOf(view, 'b1').height).toBe(2);
    expect(cardOf(view, 'b2').height).toBe(1);
    expect(cardOf(view, 'b3').height).toBe(1);
    expect(view.output.findDescendantById('event-b2-heading')).toBeUndefined();
    expect(view.output.findDescendantById('event-b3-heading')).toBeUndefined();
    // A card after them is a different speaker, so it opens a run of its own
    // and gets the divider a card gets.
    expect(cardOf(view, 'j1').border).toEqual(['top']);
    expect(cardOf(view, 'j1').height).toBe(3);
    expect(cardOf(view, 'j2').height).toBe(1);
    // One margin row (b1) + 2 + 1 + 1 + 3 + 1.
    expect(view.output.height).toBe(1 + 2 + 1 + 1 + 3 + 1);
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

/**
 * #647 review: a reviewer asked that the run id keep the card's colour
 * instead of fading to `textSubtle`, and that a bracketed source tag
 * (`[git-tracking]`, `[framework-validation]`) stand out from the rest of its
 * line. `styleSourceTags` is exported and tested directly for the same reason
 * `unwrapShellCommand` is in previews.ts: the line-splitting and anchoring is
 * the whole of the behaviour, and a full render obscures which case failed.
 */
describe('styleSourceTags (#647)', () => {
  const palette = resolveTheme(null).conversation.analysis;

  function styledChunks(content: string): {text: string; fg: string | undefined}[] {
    const styled = styleSourceTags(content, palette);
    if (typeof styled === 'string') throw new Error('expected a styled result, got a plain string');
    return styled.chunks.map(chunk => ({
      text: chunk.text,
      fg: chunk.fg === undefined ? undefined : rgbToHex(chunk.fg).toLowerCase(),
    }));
  }

  it('colors a leading tag in the label color and the rest of the line in the content color', () => {
    expect(styledChunks('[git-tracking] trusted input baseline: 4cf7a6767b6f')).toEqual([
      {text: '[git-tracking]', fg: palette.label.toLowerCase()},
      {text: ' trusted input baseline: 4cf7a6767b6f', fg: palette.content.toLowerCase()},
    ]);
  });

  it('returns untagged content unchanged, including empty content', () => {
    expect(styleSourceTags('plain line', palette)).toBe('plain line');
    expect(styleSourceTags('', palette)).toBe('');
  });

  it('leaves a bracket that is not at the start of the line untouched', () => {
    expect(styleSourceTags('see [x] here', palette)).toBe('see [x] here');
  });

  it('colors a line that is only a tag, with no trailing content chunk', () => {
    expect(styledChunks('[git-tracking]')).toEqual([
      {text: '[git-tracking]', fg: palette.label.toLowerCase()},
    ]);
  });

  it('colors each tagged line independently across multi-line content', () => {
    const content = '[git-tracking] one\nplain\n[framework-validation] two';
    expect(styledChunks(content)).toEqual([
      {text: '[git-tracking]', fg: palette.label.toLowerCase()},
      {text: ' one', fg: palette.content.toLowerCase()},
      {text: '\n', fg: palette.content.toLowerCase()},
      {text: 'plain', fg: palette.content.toLowerCase()},
      {text: '\n', fg: palette.content.toLowerCase()},
      {text: '[framework-validation]', fg: palette.label.toLowerCase()},
      {text: ' two', fg: palette.content.toLowerCase()},
    ]);
  });
});

/**
 * The two colour changes wired into `#renderEntry`: the run id and the
 * plain-text content `TextRenderable` (~480-491 and ~513-517). These render
 * through the real OpenTUI test renderer, per
 * docs/contributing/coding-best-practices.md, rather than asserting on the
 * helper alone, so a future refactor that stops passing the styled result to
 * either `TextRenderable` still fails here.
 */
describe('transcript source tags and run ids take the card label color (#647)', () => {
  it('colors the run id exactly like the role, selected or not, in every theme', async () => {
    for (const theme of listThemes()) {
      for (const selectedId of [null, 'a'] as const) {
        const {view} = await renderEntries([from(judge, 'a', 'one line')], selectedId, theme);
        try {
          const heading = view.output.findDescendantById('event-a-heading');
          if (!(heading instanceof BoxRenderable)) throw new Error('heading missing');
          const [role, runId] = heading.getChildren();
          if (!(role instanceof TextRenderable) || !(runId instanceof TextRenderable)) {
            throw new Error('heading did not render a role and a run id');
          }
          expect(rgbToHex(runId.fg).toLowerCase()).toBe(rgbToHex(role.fg).toLowerCase());
          // Pinned against textSubtle directly: a coincidental match on the
          // line above would not by itself prove textSubtle is gone.
          expect(rgbToHex(runId.fg).toLowerCase()).not.toBe(theme.textSubtle.toLowerCase());
        } finally {
          // 16 renderers (8 themes × 2 selection states) accumulate console
          // listeners if left for the shared `afterEach`; destroy each as
          // soon as it is checked instead.
          cleanup.pop()?.();
        }
      }
    }
  });

  it('colors the bracketed tag on a real diagnostic line (bad-cpp-round1.jsonl) in the label color', async () => {
    // Shape of fixture line 4: a diagnostic entry whose content is exactly
    // one `[git-tracking]`-tagged line, trailing newline included, the way
    // `agent_output_chunk` delivers it.
    const entries: ConversationEntry[] = [
      {
        id: 'd1',
        kind: 'diagnostic',
        label: 'launcher',
        content: '[git-tracking] trusted input baseline: 4cf7a6767b6f\n',
      },
    ];
    const {view} = await renderEntries(entries);
    const card = cardOf(view, 'd1');
    const text = card.getChildren().find(child => child instanceof TextRenderable);
    if (!(text instanceof TextRenderable)) throw new Error('content text missing');
    const palette = resolveTheme(null).conversation.analysis;
    const [tag, rest] = text.content.chunks;
    expect(tag?.text).toBe('[git-tracking]');
    expect(rest?.text).toBe(' trusted input baseline: 4cf7a6767b6f');
    if (tag?.fg === undefined || rest?.fg === undefined) throw new Error('chunk missing a colour');
    expect(rgbToHex(tag.fg).toLowerCase()).toBe(palette.label.toLowerCase());
    expect(rgbToHex(rest.fg).toLowerCase()).toBe(palette.content.toLowerCase());
  });

  it('leaves an untagged line as a single content-colored chunk', async () => {
    const {view} = await renderEntries([from(judge, 'a', 'plain content')]);
    const card = cardOf(view, 'a');
    const text = card.getChildren().find(child => child instanceof TextRenderable);
    if (!(text instanceof TextRenderable)) throw new Error('content text missing');
    const palette = resolveTheme(null).conversation.analysis;
    expect(text.content.chunks).toHaveLength(1);
    expect(text.content.chunks[0]?.text).toBe('plain content');
    expect(rgbToHex(text.fg).toLowerCase()).toBe(palette.content.toLowerCase());
  });
});
