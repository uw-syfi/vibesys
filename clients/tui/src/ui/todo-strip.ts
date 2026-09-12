import {BoxRenderable, type CliRenderer, TextRenderable} from '@opentui/core';
import type {TodoItem} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import type {SessionState} from '../session-model.js';
import {focusedPane, visibleTodos} from '../session-model.js';
import {paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';
import type {Theme} from './theme.js';

const STATUS_MARKER: Record<string, string> = {
  pending: '○',
  in_progress: '▶',
  completed: '✓',
};

// The todo status field is an open string on the wire; unknown statuses
// must degrade to a neutral marker, never break rendering.
const UNKNOWN_MARKER = '?';

const MAX_EXPANDED_ITEMS = 10;

/** Below this a todo line is all ellipsis, which is worse than crossing a pane edge. */
const TODO_MIN_WIDTH = 48;

/**
 * The todo box follows the agent pane it belongs under, so it stops at the
 * boundary with the transcript rather than running the width of the screen.
 * A very narrow agent pane is the one exception: a strip too thin to read says
 * nothing at all, so it keeps a readable floor.
 */
export function todoStripWidth(agentPaneWidth: number, terminalWidth: number): number {
  return Math.min(terminalWidth, Math.max(agentPaneWidth, TODO_MIN_WIDTH));
}

/**
 * The rows the strip is about to occupy for a state, derived from the state
 * rather than read back from the laid-out box. `output.height` reflects the last
 * committed layout, so it lags one paint behind a render that just changed it;
 * a sibling sized in the same paint (the rounds rail) needs the height the strip
 * is taking now, not the one it took last frame. `render` sets the box from this
 * function, so the two cannot disagree: no visible todos means no strip, a
 * collapsed strip is one summary row, and an expanded strip is its capped items
 * plus an optional overflow row inside a border.
 */
export function todoStripHeight(state: SessionState): number {
  const todos = visibleTodos(state);
  if (todos.length === 0) return 0;
  if (!state.todosExpanded) return 1;
  const shown = Math.min(todos.length, MAX_EXPANDED_ITEMS);
  const hidden = todos.length - shown;
  return shown + (hidden > 0 ? 1 : 0) + 2;
}

export function todoMarker(status: string): string {
  return STATUS_MARKER[status] ?? UNKNOWN_MARKER;
}

export function todoColor(status: string, theme: Theme): string {
  if (status === 'pending') return theme.textSubtle;
  if (status === 'in_progress') return theme.warning;
  if (status === 'completed') return theme.success;
  return theme.textMuted;
}

export function todoTitle(todos: TodoItem[]): string {
  const completed = todos.filter(todo => todo.status === 'completed').length;
  return `Todo ${completed}/${todos.length}`;
}

export function todoSummaryLine(todos: TodoItem[], maxWidth: number): string {
  const current =
    todos.find(todo => todo.status === 'in_progress') ??
    todos.find(todo => todo.status !== 'completed');
  const focus =
    current === undefined ? 'all done' : `${todoMarker(current.status)} ${current.content}`;
  return truncate(`▸ ${todoTitle(todos)} · ${focus}`, maxWidth);
}

export function todoItemLine(todo: TodoItem, maxWidth: number): string {
  return truncate(`${todoMarker(todo.status)} ${todo.content}`, maxWidth);
}

function truncate(line: string, maxWidth: number): string {
  const width = Math.max(8, maxWidth);
  return line.length <= width ? line : `${line.slice(0, width - 1)}…`;
}

/**
 * Full-width strip between the conversation viewport and the input panel.
 * Collapsed it is a one-line summary of the visible phase's todo list;
 * Ctrl+T (or a click) expands it into the full list. When the visible phase
 * has no todos the strip occupies no space at all.
 */
export class TodoStripView {
  readonly output: BoxRenderable;
  #theme: Theme;
  #renderedTodos: TodoItem[] | null = null;
  #renderedExpanded = false;
  #renderedFocused = false;
  #renderedSelection: number | null = null;
  #renderedWidth: number | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    controller: SessionController,
    theme: Theme,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'todo-strip',
      width: '100%',
      flexShrink: 0,
      height: 1,
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      visible: false,
      onMouseUp: () => controller.toggleTodos(),
    });
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.#renderedTodos = null;
  }

  render(state: SessionState, width: number | null = null): void {
    const todos = visibleTodos(state);
    if (todos.length === 0) {
      this.output.visible = false;
      this.#renderedTodos = null;
      return;
    }
    this.output.visible = true;
    // The todos belong to the agent whose transcript is on the left, so the box
    // stops where that pane stops rather than running under the right pane.
    this.output.width = width ?? '100%';
    // One source for the strip's height: siblings sized in this same paint read
    // it from `todoStripHeight` too, so the box cannot end up a row off them.
    this.output.height = todoStripHeight(state);
    // The open list owns the arrow keys, so it is a pane like any other and
    // asks the same authority whether it is the one holding them.
    const focused = focusedPane(state) === 'todos';
    if (
      todos === this.#renderedTodos &&
      state.todosExpanded === this.#renderedExpanded &&
      focused === this.#renderedFocused &&
      state.selectedTodoIndex === this.#renderedSelection &&
      width === this.#renderedWidth
    ) {
      return;
    }
    this.#renderedTodos = todos;
    this.#renderedExpanded = state.todosExpanded;
    this.#renderedFocused = focused;
    this.#renderedSelection = state.selectedTodoIndex;
    this.#renderedWidth = width;
    this.#clear();
    if (state.todosExpanded) this.#renderExpanded(todos, state.selectedTodoIndex, focused);
    else this.#renderCollapsed(todos);
  }

  #renderCollapsed(todos: TodoItem[]): void {
    this.output.add(
      new TextRenderable(this.renderer, {
        content: todoSummaryLine(todos, this.#contentWidth(false)),
        fg: this.#theme.textPrimary,
        height: 1,
        width: '100%',
      }),
    );
  }

  #renderExpanded(todos: TodoItem[], selected: number | null, focused: boolean): void {
    const shown = todos.slice(0, MAX_EXPANDED_ITEMS);
    const hidden = todos.length - shown.length;
    const height = shown.length + (hidden > 0 ? 1 : 0) + 2;
    // The border belongs to a box that only exists while the list is open.
    // Toggling a border on a live box leaves a frame the layout has no rows
    // for, and the list then draws over it.
    const list = new BoxRenderable(this.renderer, {
      id: 'todo-list',
      width: '100%',
      height,
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      borderStyle: paneBorderStyle(focused),
      borderColor: paneBorderColor(this.#theme, focused),
      title: paneTitle(todoTitle(todos), focused),
    });
    this.output.add(list);
    for (const [index, todo] of shown.entries()) {
      const isSelected = index === selected;
      list.add(
        new TextRenderable(this.renderer, {
          content: `${isSelected ? '▸' : ' '}${todoItemLine(todo, this.#contentWidth(true) - 1)}`,
          fg: isSelected ? this.#theme.textStrong : todoColor(todo.status, this.#theme),
          ...(isSelected ? {bg: this.#theme.selectedSurface} : {}),
          height: 1,
          width: '100%',
        }),
      );
    }
    if (hidden > 0) {
      list.add(
        new TextRenderable(this.renderer, {
          content: `… +${hidden} more`,
          fg: this.#theme.textSubtle,
          height: 1,
          width: '100%',
        }),
      );
    }
  }

  #contentWidth(expanded: boolean): number {
    // Expanded layout has outer padding + inner list border + inner list padding = 6 cols.
    // Collapsed layout has only 1 col of padding each side = 2 cols.
    const chrome = expanded ? 6 : 2;
    return (this.#renderedWidth ?? this.renderer.terminalWidth) - chrome;
  }

  #clear(): void {
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
  }
}
