/** The data components' output, rendered through the `.` entry. */

import {describe, expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {
  Chart,
  CheckList,
  type CheckState,
  Chip,
  Field,
  LogPane,
  Table,
  TextInput,
} from './index.js';
import {MONO_ADVANCE, TYPE} from './tokens/index.js';

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

/** The y labels of a chart: each one's text, and where its text ends and the axis begins. */
function yLabels(html: string): {
  readonly labels: readonly string[];
  readonly ends: readonly number[];
} {
  const found = [
    ...html.matchAll(
      /<text class="vs-chart-label" x="([\d.]+)"[^>]*text-anchor="end"[^>]*>([^<]*)<\/text>/g,
    ),
  ];
  return {
    labels: found.map(match => match[2] ?? ''),
    ends: found.map(match => Number(match[1])),
  };
}

describe('Chart y labels', () => {
  const formats: Readonly<Record<string, (value: number) => string>> = {
    grouped: value => value.toLocaleString('en-US', {maximumFractionDigits: 3}),
    compact: value =>
      value.toLocaleString('en-US', {notation: 'compact', maximumFractionDigits: 3}),
    plain: value => String(value),
  };

  test('are never cut by the drawing edge, for values from 1e-3 to 1e12 in any format', () => {
    const glyph = TYPE.section.size * MONO_ADVANCE;
    for (const [name, format] of Object.entries(formats)) {
      for (let exponent = -3; exponent <= 12; exponent += 1) {
        for (const mantissa of [1, 1.5, 2.5, 7.25, 9.999]) {
          const high = mantissa * 10 ** exponent;
          const yTicks = [high / 4, high / 2, high].map(value => ({value, label: format(value)}));
          const html = renderToStaticMarkup(
            <Chart label="Series" points={[{x: 1, y: high, filled: true}]} yTicks={yTicks} />,
          );
          const {labels, ends} = yLabels(html);
          expect(labels).toEqual(yTicks.map(tick => tick.label));
          labels.forEach((label, index) => {
            const start = (ends[index] ?? 0) - label.length * glyph;
            expect(start, `${name} ${label}`).toBeGreaterThanOrEqual(0);
          });
        }
      }
    }
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

describe('the pointer carries the detail behind a short word', () => {
  test('a chip carries its hint, and a field error its own', () => {
    expect(renderToStaticMarkup(<Chip label="Token found" hint="Held in a profile." />)).toContain(
      'title="Held in a profile."',
    );
    const field = renderToStaticMarkup(
      <Field label="SSH host" error="Unreachable" errorHint="Connection refused">
        <TextInput value="" change={() => undefined} />
      </Field>,
    );
    expect(field).toContain('title="Connection refused"');
    expect(field).toContain('>Unreachable<');
  });
});
