import {describe, expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {Button, type ButtonVariant, Combobox, Dialog, StatusChip, type Tone} from './index.js';

const VARIANTS: readonly ButtonVariant[] = ['default', 'primary', 'link'];
const TONES: readonly Tone[] = ['ok', 'warn', 'error', 'idle'];

describe('Button', () => {
  test('is a plain button unless the caller names another type', () => {
    expect(renderToStaticMarkup(<Button>Go</Button>)).toBe('<button type="button">Go</button>');
    expect(renderToStaticMarkup(<Button type="submit">Go</Button>)).toContain('type="submit"');
  });

  test('carries its variant and the caller class, and no class for the default', () => {
    for (const variant of VARIANTS) {
      for (const className of [undefined, 'option']) {
        const html = renderToStaticMarkup(
          <Button variant={variant} className={className}>
            Go
          </Button>,
        );
        const expected = [variant === 'default' ? '' : variant, className ?? '']
          .filter(Boolean)
          .join(' ');
        if (expected === '') expect(html).not.toContain('class=');
        else expect(html).toContain(`class="${expected}"`);
      }
    }
  });
});

describe('StatusChip', () => {
  test('is a button exactly when it can be pressed, with a dot in its tone', () => {
    for (const tone of TONES) {
      const still = renderToStaticMarkup(<StatusChip tone={tone} label="label" />);
      const pressable = renderToStaticMarkup(
        <StatusChip tone={tone} label="label" press={() => undefined} />,
      );
      const dot = tone === 'idle' ? 'class="dot"' : `class="dot ${tone}"`;
      expect(still.startsWith('<span class="chip"')).toBe(true);
      expect(pressable.startsWith('<button class="chip" type="button"')).toBe(true);
      for (const html of [still, pressable]) {
        expect(html).toContain(dot);
        expect(html).toContain('label');
      }
    }
  });
});

describe('Combobox', () => {
  test('offers each option once, in order, on the list its input names', () => {
    const html = renderToStaticMarkup(
      <Combobox listId="paths" value="/a" options={['/a', '/b', '/a']} change={() => undefined} />,
    );
    expect(html).toContain('list="paths"');
    expect(html).toContain(
      '<datalist id="paths"><option value="/a"></option><option value="/b"></option></datalist>',
    );
  });
});

describe('Dialog', () => {
  test('renders its content only while open', () => {
    for (const open of [true, false]) {
      const html = renderToStaticMarkup(
        <Dialog open={open} label="Pick" dismiss={() => undefined}>
          <p>inside</p>
        </Dialog>,
      );
      expect(html).toContain('aria-label="Pick"');
      expect(html.includes('inside')).toBe(open);
    }
  });
});
