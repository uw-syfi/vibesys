/** The data components' output, rendered through the `.` entry. */

import {describe, expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {Chart, CheckList, type CheckState, LogPane, Table} from './index.js';

describe('LogPane', () => {
  test('renders every line as text, never as markup, and marks standard error', () => {
    const html = renderToStaticMarkup(
      <LogPane
        label="Output"
        lines={[{text: '<b>bold</b> & <script>x</script>'}, {text: 'failed', stream: 'err'}]}
      />,
    );
    expect(html).toContain('&lt;b&gt;bold&lt;/b&gt; &amp; &lt;script&gt;x&lt;/script&gt;');
    expect(html).not.toContain('<b>');
    expect(html).toContain('data-stream="err"');
  });
});

describe('Chart', () => {
  test('draws each point filled or hollow, and the reference, playhead and step line', () => {
    const points = [1, 2, 3, 4, 5].map(x => ({x, y: x * 3, filled: x % 2 === 0}));
    const html = renderToStaticMarkup(
      <Chart
        label="Series"
        points={points}
        steps={[
          {x: 1, y: 3},
          {x: 5, y: 15},
        ]}
        reference={{y: 6, label: 'baseline'}}
        playhead={3}
      />,
    );
    expect(html.match(/<circle/g)?.length).toBe(points.length);
    expect(html.match(/data-filled="true"/g)?.length).toBe(2);
    for (const part of ['vs-chart-reference', 'vs-chart-playhead', 'vs-chart-step', 'baseline']) {
      expect(html).toContain(part);
    }
    expect(html).toContain('aria-label="Series"');
  });
});

describe('CheckList', () => {
  test('names each state in words beside its icon', () => {
    const states: readonly CheckState[] = ['ok', 'warn', 'fail', 'pending'];
    const html = renderToStaticMarkup(
      <CheckList items={states.map(state => ({id: state, state, label: state}))} />,
    );
    for (const word of ['Passed', 'Needs attention', 'Failed', 'Not checked yet']) {
      expect(html).toContain(`aria-label="${word}"`);
    }
  });
});

describe('Table', () => {
  test('marks the selected row current and makes rows focusable only when selectable', () => {
    const rows = [{id: 'a'}, {id: 'b'}];
    const columns = [{key: 'id', header: 'ID', cell: (row: {id: string}) => row.id}];
    const selectable = renderToStaticMarkup(
      <Table
        label="T"
        columns={columns}
        rows={rows}
        rowKey={row => row.id}
        selected="b"
        select={() => undefined}
      />,
    );
    expect(selectable.match(/aria-current="true"/g)?.length).toBe(1);
    expect(selectable.match(/<tr[^>]*tabindex="0"/g)?.length).toBe(2);
    const still = renderToStaticMarkup(
      <Table label="T" columns={columns} rows={rows} rowKey={row => row.id} />,
    );
    expect(still).not.toMatch(/<tr[^>]*tabindex/);
  });
});
