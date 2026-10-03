import {describe, expect, it} from 'bun:test';
import {type CliRenderer, KeyEvent, type ScrollBoxRenderable} from '@opentui/core';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import type {SelectionClipboard} from './clipboard.js';
import {bindKeybindings, type KeybindingActions} from './keybindings.js';

/**
 * The router under test is one plain `keypress` handler, so it is driven
 * directly: a fake key input captures the handler `bindKeybindings` registers
 * and feeds it constructed `KeyEvent`s, and a recording controller answers
 * `state` with whatever session the test poses. No rendered surface is
 * involved; what is pinned here is which handler claims a key, not what the
 * screen looks like afterwards.
 */
class FakeKeyInput {
  readonly #handlers = new Set<(key: KeyEvent) => void>();

  on(_event: 'keypress', handler: (key: KeyEvent) => void): void {
    this.#handlers.add(handler);
  }

  off(_event: 'keypress', handler: (key: KeyEvent) => void): void {
    this.#handlers.delete(handler);
  }

  emit(key: KeyEvent): void {
    for (const handler of this.#handlers) handler(key);
  }
}

function fakeRenderer(keyInput: FakeKeyInput): CliRenderer {
  return {keyInput, terminalWidth: 120, destroy: () => {}} as unknown as CliRenderer;
}

/** Answers `state` and records every method the router invokes, as a no-op. */
function recordingController(state: SessionState): {
  controller: SessionController;
  calls: string[];
} {
  const calls: string[] = [];
  const controller = new Proxy(
    {},
    {
      get(_target, property) {
        if (property === 'state') return state;
        return (): void => {
          calls.push(String(property));
        };
      },
    },
  ) as SessionController;
  return {controller, calls};
}

function stubViewport(): ScrollBoxRenderable {
  return {
    scrollTo: () => {},
    scrollBy: () => {},
    scrollHeight: 0,
  } as unknown as ScrollBoxRenderable;
}

function stubClipboard(): SelectionClipboard {
  return {copySelection: () => 'no-selection'} as unknown as SelectionClipboard;
}

function stubActions(): KeybindingActions {
  return {
    completeInput: () => false,
    navigateSuggestions: () => false,
    navigateChatSuggestions: () => false,
    completeChatInput: () => false,
    inputIsEmpty: () => true,
    closeChat: () => {},
    toggleLatestPrompt: () => {},
    toggleSelectedTool: () => false,
    revealSelectedEntry: () => {},
    revealOlderEntries: () => {},
    selectNextAgent: () => {},
    selectPreviousAgent: () => {},
    selectNextRound: () => {},
    selectPreviousRound: () => {},
    toggleTodos: () => {},
    setGraphWidthOverride: () => {},
    setChatWidthOverride: () => {},
    scrollRightPane: () => {},
    scrollChatPane: () => {},
    scrollExperimentDetail: () => {},
    scrollErrorBanner: () => {},
    scrollOverlay: () => {},
    clearTransientStatus: () => {},
    showClipboardStatus: () => {},
    runPaletteSelection: () => {},
    promoteNotepadToSteer: () => {},
    promoteNotepadToChat: () => {},
  };
}

function pressKey(name: string): KeyEvent {
  return new KeyEvent({
    name,
    ctrl: false,
    meta: false,
    shift: false,
    option: false,
    sequence: '',
    number: false,
    raw: '',
    eventType: 'press',
    source: 'raw',
  });
}

function bound(state: SessionState): {keyInput: FakeKeyInput; calls: string[]} {
  const keyInput = new FakeKeyInput();
  const {controller, calls} = recordingController(state);
  bindKeybindings(
    fakeRenderer(keyInput),
    controller,
    stubViewport(),
    stubClipboard(),
    stubActions(),
  );
  return {keyInput, calls};
}

describe('F4 pane zoom', () => {
  it('toggles the pane zoom while no modal is open', () => {
    const {keyInput, calls} = bound(initialSessionState());

    const key = pressKey('f4');
    keyInput.emit(key);

    expect(calls).toEqual(['togglePaneZoom']);
    expect(key.defaultPrevented).toBe(true);
  });

  it('is inert while the palette is open', () => {
    const {keyInput, calls} = bound({
      ...initialSessionState(),
      palette: {query: '', selected: 0},
    });

    const key = pressKey('f4');
    keyInput.emit(key);

    expect(calls).not.toContain('togglePaneZoom');
    // The palette is modal: the key is swallowed rather than left to fall
    // through to the panes, the same claim every other modal already has.
    expect(key.defaultPrevented).toBe(true);
  });
});
