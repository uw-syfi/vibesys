import {afterEach, describe, expect, it} from 'bun:test';
import {type Renderable, TextRenderable} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {TodoItem} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {displayWidth} from './text-width.js';
import {resolveTheme} from './theme.js';
import {TodoStripView, todoItemLine, todoSummaryLine} from './todo-strip.js';

describe('todo strip formatting', () => {
  it('focuses the summary on the in-progress item', () => {
    const line = todoSummaryLine(
      [
        {content: 'Set up project', status: 'completed'},
        {content: 'Vectorize the kernel', status: 'in_progress'},
        {content: 'Add tests', status: 'pending'},
      ],
      80,
    );
    expect(line).toBe('▸ Todo 1/3 · ▶ Vectorize the kernel');
  });

  it('falls back to the next pending item and then to all done', () => {
    expect(
      todoSummaryLine(
        [
          {content: 'Set up project', status: 'completed'},
          {content: 'Add tests', status: 'pending'},
        ],
        80,
      ),
    ).toBe('▸ Todo 1/2 · ○ Add tests');
    expect(todoSummaryLine([{content: 'Set up project', status: 'completed'}], 80)).toBe(
      '▸ Todo 1/1 · all done',
    );
  });

  it('degrades unknown statuses to a neutral marker instead of failing', () => {
    expect(todoItemLine({content: 'Mystery step', status: 'deferred'}, 80)).toBe('? Mystery step');
  });

  it('truncates long lines to the available width with an ellipsis', () => {
    const line = todoItemLine({content: 'x'.repeat(50), status: 'pending'}, 20);
    expect(line).toHaveLength(20);
    expect(line.endsWith('…')).toBe(true);
  });

  it('truncates a CJK label by display cells, not code units', () => {
    // Nine ideographs are nine code units but eighteen cells: measured by
    // `String.length` this line fits a 13-cell budget and overflows it on
    // screen by seven cells.
    const line = todoItemLine({content: '优化内核向量化路径', status: 'in_progress'}, 13);
    expect(line).toBe('▶ 优化内核向…');
    expect(displayWidth(line)).toBe(13);
  });

  it('drops a wide character that would straddle the budget instead of splitting it', () => {
    const line = todoItemLine({content: '优化内核向量化路径', status: 'in_progress'}, 14);
    // One cell short of the budget: the next ideograph needs two cells and
    // only one is left, so it goes whole rather than as half a glyph.
    expect(line).toBe('▶ 优化内核向…');
    expect(displayWidth(line)).toBe(13);
  });
});

/**
 * The formatters above are pure and take the width they are told to take, so
 * they cannot see a wrong width. The chrome arithmetic that picks that width is
 * private to the view, so these tests render a strip at a fixed box width and
 * compare the emitted text against the slot the layout actually gave it: a line
 * that stops short of its own text renderable is a strip that stops short of
 * the box edge.
 */
describe('todo strip rendering', () => {
  const cleanup: Array<() => void> = [];

  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
  });

  const BOX_WIDTH = 60;
  const LONG_TODO: TodoItem = {content: 'x'.repeat(200), status: 'in_progress'};

  function stateWith(todos: TodoItem[], expanded: boolean): SessionState {
    const base = initialSessionState();
    return {
      ...base,
      core: {...base.core, todos: [{agentKind: null, roundNumber: null, items: todos}]},
      todosExpanded: expanded,
    };
  }

  /** The view only calls back on a click, which none of these tests perform. */
  const controller = {
    toggleTodos: () => {},
  } as unknown as SessionController;

  interface RenderedLine {
    text: string;
    /** Columns the layout gave the line, i.e. how wide it was allowed to be. */
    width: number;
  }

  function renderedLines(node: Renderable, out: RenderedLine[] = []): RenderedLine[] {
    for (const child of node.getChildren()) {
      if (child instanceof TextRenderable) {
        out.push({
          text: child.content.chunks.map(chunk => chunk.text).join(''),
          width: child.width,
        });
      } else renderedLines(child, out);
    }
    return out;
  }

  function onlyLine(lines: RenderedLine[]): RenderedLine {
    expect(lines).toHaveLength(1);
    const [line] = lines;
    if (line === undefined) throw new Error('the strip emitted no text');
    return line;
  }

  async function renderStrip(
    todos: TodoItem[],
    expanded: boolean,
    boxWidth: number,
  ): Promise<{lines: RenderedLine[]; frame: string}> {
    const testRenderer: TestRendererSetup = await createTestRenderer({width: 80, height: 24});
    const strip = new TodoStripView(testRenderer.renderer, controller, resolveTheme(null));
    testRenderer.renderer.root.add(strip.output);
    cleanup.push(() => {
      strip.output.destroyRecursively();
      testRenderer.renderer.destroy();
    });
    strip.render(stateWith(todos, expanded), boxWidth);
    await testRenderer.renderOnce();
    return {lines: renderedLines(strip.output), frame: testRenderer.captureCharFrame()};
  }

  it('fills the collapsed summary to the box edge', async () => {
    const {lines} = await renderStrip([LONG_TODO], false, BOX_WIDTH);
    const summary = onlyLine(lines);
    // 1 col of padding each side is the collapsed layout's only chrome.
    expect(summary.width).toBe(BOX_WIDTH - 2);
    expect(summary.text).toHaveLength(summary.width);
    expect(summary.text.endsWith('…')).toBe(true);
  });

  it('fills the expanded item to the inner list edge', async () => {
    const {lines} = await renderStrip([LONG_TODO], true, BOX_WIDTH);
    const item = onlyLine(lines);
    // Outer padding + inner list border + inner list padding = 6 cols; the
    // selection marker is part of the line, not chrome the line gives up.
    expect(item.width).toBe(BOX_WIDTH - 6);
    expect(item.text).toHaveLength(item.width);
    expect(item.text.endsWith('…')).toBe(true);
  });

  it('fills the collapsed summary to the box edge in display cells for a CJK todo', async () => {
    const cjkTodo: TodoItem = {content: '优'.repeat(200), status: 'in_progress'};
    const {lines} = await renderStrip([cjkTodo], false, BOX_WIDTH);
    const summary = onlyLine(lines);
    expect(summary.width).toBe(BOX_WIDTH - 2);
    // Cells, not code units: sliced by code units this line would occupy
    // almost twice its slot and spill past the box edge.
    expect(displayWidth(summary.text)).toBe(summary.width);
    expect(summary.text.endsWith('…')).toBe(true);
  });

  it('survives a box narrower than its own chrome', async () => {
    for (const boxWidth of [8, 2]) {
      const collapsed = await renderStrip([LONG_TODO], false, boxWidth);
      for (const row of collapsed.frame.split('\n')) {
        expect(row.trimEnd().length).toBeLessThanOrEqual(boxWidth);
      }
      // The expanded list carries a rounded border with a 4-column floor of its
      // own, which a live strip never reaches because todoStripWidth() keeps the
      // box at TODO_MIN_WIDTH or wider. Assert only that it renders and stays on
      // screen.
      const expanded = await renderStrip([LONG_TODO], true, boxWidth);
      expect(expanded.lines).toHaveLength(1);
      for (const row of expanded.frame.split('\n')) {
        expect(row.trimEnd().length).toBeLessThanOrEqual(80);
      }
    }
  });
});
