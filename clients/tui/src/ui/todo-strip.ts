import {BoxRenderable, type CliRenderer, TextRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import type {SessionState, TodoItem} from '../session-model.js';
import {visibleTodos} from '../session-model.js';

const STATUS_MARKER: Record<string, string> = {
  pending: '○',
  in_progress: '▶',
  completed: '✓',
};

const STATUS_COLOR: Record<string, string> = {
  pending: '#64748b',
  in_progress: '#facc15',
  completed: '#4ade80',
};

// The todo status field is an open string on the wire; unknown statuses
// must degrade to a neutral marker, never break rendering.
const UNKNOWN_MARKER = '?';
const UNKNOWN_COLOR = '#94a3b8';

const MAX_EXPANDED_ITEMS = 10;

export function todoMarker(status: string): string {
  return STATUS_MARKER[status] ?? UNKNOWN_MARKER;
}

export function todoColor(status: string): string {
  return STATUS_COLOR[status] ?? UNKNOWN_COLOR;
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
  #renderedTodos: TodoItem[] | null = null;
  #renderedExpanded = false;

  constructor(
    private readonly renderer: CliRenderer,
    controller: SessionController,
  ) {
    this.output = new BoxRenderable(renderer, {
      id: 'todo-strip',
      width: '100%',
      height: 1,
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      borderStyle: 'rounded',
      borderColor: '#334155',
      border: false,
      visible: false,
      onMouseUp: () => controller.toggleTodos(),
    });
  }

  render(state: SessionState): void {
    const todos = visibleTodos(state);
    if (todos.length === 0) {
      this.output.visible = false;
      this.#renderedTodos = null;
      return;
    }
    this.output.visible = true;
    if (todos === this.#renderedTodos && state.todosExpanded === this.#renderedExpanded) return;
    this.#renderedTodos = todos;
    this.#renderedExpanded = state.todosExpanded;
    this.#clear();
    if (state.todosExpanded) this.#renderExpanded(todos);
    else this.#renderCollapsed(todos);
  }

  #renderCollapsed(todos: TodoItem[]): void {
    this.output.border = false;
    this.output.height = 1;
    this.output.add(
      new TextRenderable(this.renderer, {
        content: todoSummaryLine(todos, this.#contentWidth()),
        fg: '#cbd5e1',
        height: 1,
        width: '100%',
      }),
    );
  }

  #renderExpanded(todos: TodoItem[]): void {
    const shown = todos.slice(0, MAX_EXPANDED_ITEMS);
    const hidden = todos.length - shown.length;
    this.output.border = true;
    this.output.title = ` ${todoTitle(todos)} `;
    this.output.height = shown.length + (hidden > 0 ? 1 : 0) + 2;
    for (const todo of shown) {
      this.output.add(
        new TextRenderable(this.renderer, {
          content: todoItemLine(todo, this.#contentWidth()),
          fg: todoColor(todo.status),
          height: 1,
          width: '100%',
        }),
      );
    }
    if (hidden > 0) {
      this.output.add(
        new TextRenderable(this.renderer, {
          content: `… +${hidden} more`,
          fg: '#64748b',
          height: 1,
          width: '100%',
        }),
      );
    }
  }

  #contentWidth(): number {
    return this.renderer.terminalWidth - 6;
  }

  #clear(): void {
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
  }
}
