import {describe, expect, it} from 'vitest';
import {todoItemLine, todoSummaryLine} from './todo-strip.js';

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
});
