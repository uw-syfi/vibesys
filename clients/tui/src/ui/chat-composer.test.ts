import {afterEach, describe, expect, it, setSystemTime} from 'bun:test';
import {BoxRenderable, type Renderable, TextareaRenderable} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {ChatComposerView, createChatDraft, pendingComposerLabel} from './chat-composer.js';
import {ChatPaneView} from './chat-pane.js';
import {paneTitle} from './focus.js';
import {createMarkdownStyle} from './styles.js';
import {resolveTheme} from './theme.js';

describe('pending composer title', () => {
  it('shows a spinner frame and the elapsed wait', () => {
    expect(pendingComposerLabel(0, 0)).toBe('Message · ⠋ 0s');
    expect(pendingComposerLabel(1, 5_000)).toBe('Message · ⠙ 5s');
  });

  it('wraps the frame index so a long wait keeps animating', () => {
    expect(pendingComposerLabel(10, 1_000)).toBe(pendingComposerLabel(0, 1_000));
    expect(pendingComposerLabel(23, 1_000)).toBe(pendingComposerLabel(3, 1_000));
  });
});

/**
 * The spinner's state lives in the composer, but only the surfaces around it
 * know whether they are on screen, so these drive a real `ChatPaneView` and
 * read the title its box ended up with. A question that completes while the
 * dock is hidden is the case `activate` alone cannot see: it does not run
 * while hidden, so nothing stops the timer or moves the elapsed epoch.
 */
describe('composer spinner across a hidden surface', () => {
  const cleanup: Array<() => void> = [];
  const EPOCH = Date.UTC(2026, 8, 3, 12, 0, 0);

  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
    setSystemTime();
  });

  /** The composer only calls back on input, which none of these tests send. */
  const controller = {
    focusPane: () => {},
    submitChat: () => {},
  } as unknown as SessionController;

  function stateWith(pending: boolean): SessionState {
    return {...initialSessionState(), chatPending: pending};
  }

  function composerTitle(node: Renderable): string | null {
    for (const child of node.getChildren()) {
      if (child instanceof BoxRenderable && child.id.endsWith('-composer-box')) {
        return child.title ?? null;
      }
      const found = composerTitle(child);
      if (found !== null) return found;
    }
    return null;
  }

  interface Dock {
    render: (pending: boolean, visible: boolean) => void;
    title: () => string | null;
    destroy: () => void;
  }

  async function dockedChat(): Promise<Dock> {
    const testRenderer: TestRendererSetup = await createTestRenderer({width: 120, height: 24});
    const theme = resolveTheme(null);
    const markdownStyle = createMarkdownStyle(theme);
    const view = new ChatPaneView(
      testRenderer.renderer,
      controller,
      markdownStyle,
      theme,
      createChatDraft(),
    );
    testRenderer.renderer.root.add(view.output);
    cleanup.push(() => {
      view.destroy();
      view.output.destroyRecursively();
      markdownStyle.destroy();
      testRenderer.renderer.destroy();
    });
    return {
      render: (pending, visible) => view.render(stateWith(pending), visible, 40),
      title: () => composerTitle(view.output),
      destroy: () => view.destroy(),
    };
  }

  it('clears the spinner when the answer lands while the dock is hidden', async () => {
    const dock = await dockedChat();
    setSystemTime(new Date(EPOCH));
    dock.render(true, true);
    expect(dock.title()).toBe(paneTitle(pendingComposerLabel(0, 0), false));

    dock.render(true, false);
    dock.render(false, false);

    expect(dock.title()).toBe(paneTitle('Message', false));
  });

  it('restarts the elapsed wait for the question asked after a hidden one', async () => {
    const dock = await dockedChat();
    setSystemTime(new Date(EPOCH));
    dock.render(true, true);
    setSystemTime(new Date(EPOCH + 30_000));
    dock.render(true, false);
    dock.render(false, false);

    setSystemTime(new Date(EPOCH + 40_000));
    dock.render(true, true);

    // The new question's wait, not the one that ran out of sight.
    expect(dock.title()).toBe(paneTitle(pendingComposerLabel(0, 0), false));
  });

  it('animates again after a destroy rather than staying frozen', async () => {
    const dock = await dockedChat();
    setSystemTime(new Date(EPOCH));
    dock.render(true, true);
    dock.destroy();

    setSystemTime(new Date(EPOCH + 7_000));
    dock.render(true, true);

    expect(dock.title()).toBe(paneTitle(pendingComposerLabel(0, 0), false));
  });
});

/**
 * The editor's height must count the rows the textarea actually wraps the
 * draft into, not an estimate over code points: the textarea wraps over
 * display cells (a CJK character is two) and breaks on word boundaries (a
 * word that would straddle the edge moves whole to the next row). An
 * undercounted box scrolls the draft's first rows out of a fixed-height
 * textarea while MAX_EDITOR_ROWS still has room, which is the symptom
 * issue #427 was about.
 */
describe('composer height against the word-wrapped draft', () => {
  const cleanup: Array<() => void> = [];

  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
  });

  interface SizedComposer {
    setDraft: (value: string) => Promise<void>;
    editorHeight: () => number;
    frame: () => string;
  }

  /**
   * A composer mounted alone on an `availableWidth`-column terminal: its box
   * borders and padding leave the textarea four cells fewer, and `activate`
   * names the same terminal width, so the height math and the textarea wrap
   * against equal widths.
   */
  async function sizedComposer(availableWidth = 60): Promise<SizedComposer> {
    const testRenderer: TestRendererSetup = await createTestRenderer({
      width: availableWidth,
      height: 12,
    });
    const view = new ChatComposerView(
      testRenderer.renderer,
      createChatDraft(),
      () => {},
      resolveTheme(null),
      'sized',
    );
    testRenderer.renderer.root.add(view.output);
    view.activate(availableWidth, true, false);
    await testRenderer.renderOnce();
    cleanup.push(() => {
      view.destroy();
      view.output.destroyRecursively();
      testRenderer.renderer.destroy();
    });
    const editor = testRenderer.renderer.root.findDescendantById('sized-composer-editor');
    if (!(editor instanceof TextareaRenderable)) throw new Error('composer editor was missing');
    return {
      setDraft: async value => {
        editor.focus();
        editor.setText(value);
        // The cursor at the end, where typing leaves it: an undersized box
        // then scrolls the head of the draft out rather than the tail.
        editor.gotoBufferEnd();
        // One frame to deliver the content change, one to lay out the height
        // it set.
        await testRenderer.renderOnce();
        await testRenderer.renderOnce();
      },
      editorHeight: () => editor.height,
      frame: () => testRenderer.captureCharFrame(),
    };
  }

  it('keeps every row of a CJK draft on screen', async () => {
    const composer = await sizedComposer();
    // 30 cells per sentence: two per row would take 60, so each of the four
    // sentences word-wraps onto its own 56-cell row. Counting code points
    // instead (60 over 56) says two rows.
    const sentence = '実験のまとめを教えてください。';
    await composer.setDraft(sentence.repeat(4));

    expect(composer.editorHeight()).toBe(4);
    const rows = composer
      .frame()
      .split('\n')
      .filter(row => row.includes(sentence));
    expect(rows).toHaveLength(4);
  });

  it('counts rows the way word wrap breaks them, not by character fill', async () => {
    const composer = await sizedComposer();
    // Four 30-character words: two never share a 56-cell row, so word wrap
    // takes four rows, while character fill (123 code points over 56) says
    // three.
    const words = ['a', 'b', 'c', 'd'].map(letter => letter.repeat(30));
    await composer.setDraft(words.join(' '));

    expect(composer.editorHeight()).toBe(4);
    // The first word is still on screen: an undercounted box would have
    // scrolled it out to keep the cursor's row visible.
    expect(composer.frame()).toContain('a'.repeat(30));
  });

  it('leaves a short draft at a single row', async () => {
    const composer = await sizedComposer();
    await composer.setDraft('hi there');

    expect(composer.editorHeight()).toBe(1);
  });

  it('still clamps a long draft at the row cap', async () => {
    const composer = await sizedComposer();
    await composer.setDraft('実験のまとめを教えてください。'.repeat(8));

    expect(composer.editorHeight()).toBe(6);
  });

  it('never sizes the empty composer for its wrapping placeholder', async () => {
    // Twenty columns leave the textarea 16, where the placeholder itself
    // wraps to two rows. The empty editor still rests at one: the
    // measurement reads the edit buffer's view, which shows the placeholder
    // when the buffer is empty, and that row count must not become the box.
    const composer = await sizedComposer(20);

    expect(composer.editorHeight()).toBe(1);

    await composer.setDraft('hi');
    await composer.setDraft('');
    expect(composer.editorHeight()).toBe(1);
  });
});
