import {describe, expect, it} from 'bun:test';
import {BoxRenderable, InputRenderable, rgbToHex, TextRenderable} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import {initialSessionState} from '../session-model.js';
import {createCommandInputPanel} from './command-input.js';
import {paneBorderColor} from './focus.js';
import {resolveTheme, type Theme} from './theme.js';

interface Mounted {
  testRenderer: TestRendererSetup;
  hint: () => TextRenderable;
  box: () => BoxRenderable;
  input: () => InputRenderable;
  render: (inputError: string | null) => void;
  destroy: () => void;
}

/** `.content` is `StyledText`, not a plain string; flatten it for assertions. */
function text(node: TextRenderable): string {
  return node.content.chunks.map(chunk => chunk.text).join('');
}

async function mount(theme: Theme, onChange: () => void = () => {}): Promise<Mounted> {
  const testRenderer = await createTestRenderer({width: 60, height: 10});
  const panel = createCommandInputPanel(
    testRenderer.renderer,
    () => {},
    theme,
    () => {},
    onChange,
  );
  testRenderer.renderer.root.add(panel.output);
  const find = <T>(id: string, ctor: new (...args: never[]) => T): T => {
    const node = testRenderer.renderer.root.findDescendantById(id);
    if (!(node instanceof ctor)) throw new Error(`${id} was missing`);
    return node;
  };
  return {
    testRenderer,
    hint: () => find('command-input-hint', TextRenderable),
    box: () => find('command-input-box', BoxRenderable),
    input: () => find('command-input', InputRenderable),
    render: inputError => panel.render({...initialSessionState(), inputError}),
    destroy: () => {
      panel.destroy();
      panel.output.destroyRecursively();
      testRenderer.renderer.destroy();
    },
  };
}

describe('command input hint row', () => {
  it('is reserved at rest, with useful key hints', async () => {
    const theme = resolveTheme('dark');
    const {hint, destroy} = await mount(theme);
    try {
      expect(text(hint())).toBe('Enter: run · Tab: complete');
    } finally {
      destroy();
    }
  });

  it('keeps the same height at rest and while an error stands', async () => {
    const theme = resolveTheme('dark');
    const {testRenderer, render, destroy} = await mount(theme);
    try {
      const panelHeight = (): number | undefined =>
        testRenderer.renderer.root.findDescendantById('command-input-panel')?.height;

      const restingHeight = panelHeight();
      render('Unknown command: /nope. Use /help.');
      expect(panelHeight()).toBe(restingHeight);
      render(null);
      expect(panelHeight()).toBe(restingHeight);
    } finally {
      destroy();
    }
  });

  it('shows the message with a non-colour glyph and the box border turns theme.error', async () => {
    const theme = resolveTheme('dark');
    const {hint, box, render, destroy} = await mount(theme);
    try {
      const restingColor = paneBorderColor(theme, false).toLowerCase();
      expect(rgbToHex(box().borderColor).toLowerCase()).toBe(restingColor);

      render('Commands start with /. Use Experiment chat for questions.');
      expect(text(hint())).toBe('✗ Commands start with /. Use Experiment chat for questions.');
      expect(rgbToHex(hint().fg).toLowerCase()).toBe(theme.error.toLowerCase());
      expect(rgbToHex(box().borderColor).toLowerCase()).toBe(theme.error.toLowerCase());

      render(null);
      expect(text(hint())).toBe('Enter: run · Tab: complete');
      expect(rgbToHex(box().borderColor).toLowerCase()).toBe(restingColor);
    } finally {
      destroy();
    }
  });

  it('renders the glyph and the error colour under a high-contrast theme', async () => {
    const theme = resolveTheme('high-contrast-dark');
    const {hint, box, render, destroy} = await mount(theme);
    try {
      render('Usage: /pause');
      expect(text(hint())).toContain('✗');
      expect(rgbToHex(hint().fg).toLowerCase()).toBe(theme.error.toLowerCase());
      expect(rgbToHex(box().borderColor).toLowerCase()).toBe(theme.error.toLowerCase());
    } finally {
      destroy();
    }
  });

  it('notifies on every keystroke, so a stale error can be cleared by the caller', async () => {
    let changes = 0;
    const theme = resolveTheme('dark');
    const {input, destroy} = await mount(theme, () => {
      changes += 1;
    });
    try {
      input().insertText('/');
      expect(changes).toBe(1);
      input().insertText('h');
      expect(changes).toBe(2);
    } finally {
      destroy();
    }
  });
});
