import {afterEach, describe, expect, it} from 'bun:test';
import {
  BoxRenderable,
  type CapturedFrame,
  type Renderable,
  rgbToHex,
  TextRenderable,
} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {SessionController} from '../session-controller.js';
import type {ConversationEntry, SessionState} from '../session-model.js';
import {initialSessionState} from '../session-model.js';
import {ConversationView} from './conversation.js';
import {createMarkdownStyle} from './styles.js';
import {
  CONVERSATION_ROLES,
  type ConversationRole,
  contrastRatio,
  listThemes,
  resolveTheme,
  type Theme,
} from './theme.js';

const cleanup: (() => void)[] = [];

afterEach(() => {
  for (const dispose of cleanup.splice(0)) dispose();
});

/** The glyph the role stripe is drawn with. */
const STRIPE = '▌';

/**
 * One entry per conversation role. `entryPalette` resolves the role from the
 * kind and the tone, so this is the shortest transcript that covers all eight.
 * None of them is a tool turn with a call and a response, which draws its own
 * bands and would confuse a question about the card's own fill.
 */
const ROLE_ENTRIES: Record<ConversationRole, ConversationEntry> = {
  assistant: {id: 'assistant', kind: 'assistant', content: 'assistant body', label: 'Assistant'},
  user: {id: 'user', kind: 'user', content: 'user body', label: 'User'},
  prompt: {id: 'prompt', kind: 'prompt', content: 'prompt body', label: 'Prompt'},
  analysis: {id: 'analysis', kind: 'analysis', content: 'analysis body', label: 'Analysis'},
  tool: {id: 'tool', kind: 'tool', content: 'tool body', label: 'Tool'},
  neutral: {id: 'neutral', kind: 'result', content: 'result body', label: 'Result'},
  success: {
    id: 'success',
    kind: 'result',
    content: 'passed',
    label: 'Judge',
    tone: 'success',
  },
  failure: {
    id: 'failure',
    kind: 'result',
    content: 'failed',
    label: 'Judge',
    tone: 'failure',
  },
};

const STATUS_ENTRY: ConversationEntry = {
  id: 'status',
  kind: 'status',
  content: 'started',
  label: 'implementer',
};

/**
 * The view reads `state` and calls back into the controller only from mouse
 * handlers, which these tests never fire. Anything else being reached is a
 * fact about the view worth failing on rather than stubbing away.
 */
function stubController(state: SessionState): SessionController {
  return new Proxy(
    {},
    {
      get(_target, property): unknown {
        if (property === 'state') return state;
        return () => {
          throw new Error(`ConversationView called controller.${String(property)}`);
        };
      },
    },
  ) as SessionController;
}

interface Mounted {
  testRenderer: TestRendererSetup;
  view: ConversationView;
  theme: Theme;
}

/**
 * Mounts a transcript on a canvas the size of the real one, with markdown off
 * so every card takes the verbatim-content path: the markdown renderable draws
 * asynchronously, and none of these assertions is about markdown.
 */
async function mountTranscript(
  entries: ConversationEntry[],
  theme: Theme,
  selectedEntryId: string | null = null,
): Promise<Mounted> {
  const testRenderer = await createTestRenderer({width: 60, height: 44});
  const initial = initialSessionState(theme.name);
  const state: SessionState = {
    ...initial,
    selectedEntryId,
    // The run has ended, so the transcript is not streaming. `core.terminal`
    // was replaced by the status-derived `hasRunEnded` in #572.
    core: {...initial.core, status: 'completed' as const},
  };
  const canvas = new BoxRenderable(testRenderer.renderer, {
    id: 'canvas',
    width: '100%',
    height: '100%',
    flexDirection: 'column',
    backgroundColor: theme.canvas,
  });
  testRenderer.renderer.root.add(canvas);
  const view = new ConversationView(
    testRenderer.renderer,
    stubController(state),
    createMarkdownStyle(theme),
    theme,
    {
      // Not `state.core.transcript`: a user turn only ever reaches a chat
      // conversation, so the run transcript's element type excludes it.
      selectConversation: () => entries,
      markdownKinds: [],
      showsSelection: true,
    },
  );
  canvas.add(view.output);
  view.render(state);
  await testRenderer.renderOnce();
  cleanup.push(() => testRenderer.renderer.destroy());
  return {testRenderer, view, theme};
}

interface Cell {
  char: string;
  fg: string;
  bg: string;
}

/** Explodes a captured frame into cells so a single column can be inspected. */
function cells(frame: CapturedFrame): Cell[][] {
  return frame.lines.map(line => {
    const row: Cell[] = [];
    for (const span of line.spans) {
      const fg = rgbToHex(span.fg).toLowerCase();
      const bg = rgbToHex(span.bg).toLowerCase();
      for (const char of [...span.text]) row.push({char, fg, bg});
    }
    return row;
  });
}

/** Every renderable under the mounted view, cards and their contents alike. */
function* descendants(mounted: Mounted): Generator<Renderable> {
  const pending: Renderable[] = [mounted.view.output];
  while (pending.length > 0) {
    const node = pending.pop();
    if (node === undefined) continue;
    yield node;
    pending.push(...node.getChildren());
  }
}

function cardBox(mounted: Mounted, id: string): BoxRenderable {
  const card = mounted.testRenderer.renderer.root.findDescendantById(`event-${id}`);
  if (!(card instanceof BoxRenderable)) throw new Error(`no card for ${id}`);
  return card;
}

/** The cell the stripe column holds on a card's first content row. */
function stripeCell(mounted: Mounted, id: string): Cell {
  const card = cardBox(mounted, id);
  const grid = cells(mounted.testRenderer.captureSpans());
  const cell = grid[card.y + 1]?.[card.x + 1];
  if (cell === undefined) throw new Error(`card ${id} is off screen`);
  return cell;
}

describe('transcript card role stripe', () => {
  it('draws the role on the cards left edge, in the role colour', async () => {
    const theme = resolveTheme('dark');
    const mounted = await mountTranscript(Object.values(ROLE_ENTRIES), theme);
    for (const role of CONVERSATION_ROLES) {
      const entry = ROLE_ENTRIES[role];
      const cell = stripeCell(mounted, entry.id);
      expect(cell.char, `${role} stripe glyph`).toBe(STRIPE);
      expect(cell.fg, `${role} stripe colour`).toBe(theme.conversation[role].label.toLowerCase());
    }
  });

  it('runs the stripe the whole height of the card', async () => {
    const theme = resolveTheme('dark');
    const entry: ConversationEntry = {
      id: 'tall',
      kind: 'assistant',
      label: 'Assistant',
      content: 'one\ntwo\nthree\nfour',
    };
    const mounted = await mountTranscript([entry], theme);
    const card = cardBox(mounted, 'tall');
    const grid = cells(mounted.testRenderer.captureSpans());
    // Every row between the card's top and bottom frame carries the stripe, so
    // the role reads down the card rather than only beside its heading.
    for (let row = card.y + 1; row < card.y + card.height - 1; row += 1) {
      expect(grid[row]?.[card.x + 1]?.char, `row ${row - card.y}`).toBe(STRIPE);
    }
  });

  it('keeps the stripe on the selected card, whose frame carries the cursor', async () => {
    const theme = resolveTheme('dark');
    const mounted = await mountTranscript(Object.values(ROLE_ENTRIES), theme, 'analysis');
    const card = cardBox(mounted, 'analysis');
    const grid = cells(mounted.testRenderer.captureSpans());
    const stripe = grid[card.y + 1]?.[card.x + 1];
    expect(stripe?.char).toBe(STRIPE);
    expect(stripe?.fg).toBe(theme.conversation.analysis.label.toLowerCase());
    // Selection owns the frame and only the frame.
    expect(grid[card.y]?.[card.x]?.fg).toBe(theme.borderFocus.toLowerCase());
    expect(grid[card.y]?.[card.x]?.fg).not.toBe(
      grid[cardBox(mounted, 'user').y]?.[card.x]?.fg ?? '',
    );
  });

  it('paints no role fill behind the card', async () => {
    const theme = resolveTheme('dark');
    const mounted = await mountTranscript(Object.values(ROLE_ENTRIES), theme);
    const grid = cells(mounted.testRenderer.captureSpans());
    const fills = new Set(
      CONVERSATION_ROLES.map(role => theme.conversation[role].background.toLowerCase()),
    );
    const painted = new Set(grid.flatMap(row => row.map(cell => cell.bg)));
    expect([...painted].filter(bg => fills.has(bg))).toEqual([]);
    // Positive control: the cards are on screen and sit on the canvas.
    expect(painted).toContain(theme.canvas.toLowerCase());
    for (const role of CONVERSATION_ROLES) {
      const card = cardBox(mounted, ROLE_ENTRIES[role].id);
      const row = grid[card.y + 1] ?? [];
      const backgrounds = new Set(row.slice(card.x, card.x + card.width).map(cell => cell.bg));
      expect([...backgrounds], `${role} card row`).toEqual([theme.canvas.toLowerCase()]);
    }
  });
});

describe('transcript card alignment', () => {
  it('reserves the stripe column rather than adding one', async () => {
    const theme = resolveTheme('dark');
    const mounted = await mountTranscript([...Object.values(ROLE_ENTRIES), STATUS_ENTRY], theme);
    const grid = cells(mounted.testRenderer.captureSpans());
    for (const role of CONVERSATION_ROLES) {
      const entry = ROLE_ENTRIES[role];
      const card = cardBox(mounted, entry.id);
      const heading = mounted.testRenderer.renderer.root.findDescendantById(
        `event-${entry.id}-heading`,
      );
      // Frame, stripe, then text: the same column the card's left padding used
      // to put text in, so the stripe costs no cell.
      expect(heading?.x, `${role} heading column`).toBe(card.x + 2);
      const label = (entry.label ?? '').slice(0, 4);
      expect(
        grid[card.y + 1]
          ?.slice(card.x + 2, card.x + 2 + label.length)
          .map(cell => cell.char)
          .join(''),
        `${role} heading text`,
      ).toBe(label);
    }
    // A status line reports on the run rather than speaking in it, so it gets
    // no stripe. It still reserves the column, so it lines up with the cards
    // around it instead of hanging a column to their left.
    const status = cardBox(mounted, STATUS_ENTRY.id);
    const statusHeading = mounted.testRenderer.renderer.root.findDescendantById(
      `event-${STATUS_ENTRY.id}-heading`,
    );
    expect(statusHeading?.x).toBe(status.x + 2);
    expect(grid[status.y + 1]?.[status.x + 1]?.char).toBe(' ');
  });

  it('puts every card body in the same column, striped or not', async () => {
    const theme = resolveTheme('dark');
    const entries = [...Object.values(ROLE_ENTRIES), STATUS_ENTRY];
    const mounted = await mountTranscript(entries, theme);
    const columns = new Set(
      entries.map(entry => {
        const card = cardBox(mounted, entry.id);
        const heading = mounted.testRenderer.renderer.root.findDescendantById(
          `event-${entry.id}-heading`,
        );
        return (heading?.x ?? -1) - card.x;
      }),
    );
    expect(columns).toEqual(new Set([2]));
  });

  it('draws the stripe as a reserved column, not as text', async () => {
    const theme = resolveTheme('dark');
    const mounted = await mountTranscript([ROLE_ENTRIES.assistant], theme, 'assistant');
    expect(stripeCell(mounted, 'assistant').char).toBe(STRIPE);
    // Prefixed to the content instead, the glyph would indent with wrapped
    // lines, move when the cursor marker appears, and land in a copied
    // selection.
    const written = [...descendants(mounted)]
      .filter(node => node instanceof TextRenderable)
      .map(node => node.content.toString());
    expect(written.length).toBeGreaterThan(0);
    expect(written.filter(content => content.includes(STRIPE))).toEqual([]);
  });
});

describe('role stripes across themes', () => {
  it.each(
    listThemes().map(theme => [theme.name, theme] as const),
  )('%s tells every role apart and keeps the stripe legible', async (_name, theme: Theme) => {
    const mounted = await mountTranscript(Object.values(ROLE_ENTRIES), theme);
    const stripes = new Map<ConversationRole, string>();
    for (const role of CONVERSATION_ROLES) {
      const cell = stripeCell(mounted, ROLE_ENTRIES[role].id);
      expect(cell.char, `${role} stripe glyph`).toBe(STRIPE);
      // 3:1 is the floor for a graphical element. Several raw role accents
      // sit under it against their theme's canvas, which is why the stripe
      // takes the derived label colour instead.
      expect(contrastRatio(cell.fg, theme.canvas), `${role} stripe contrast`).toBeGreaterThan(3);
      stripes.set(role, cell.fg);
    }
    expect(new Set(stripes.values()).size, 'distinct role stripes').toBe(CONVERSATION_ROLES.length);
  });
});
