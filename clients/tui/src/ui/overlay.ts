import {BoxRenderable, type CliRenderer, TextRenderable} from '@opentui/core';
import type {SessionState} from '../session-model.js';

const TITLE: Record<NonNullable<SessionState['overlay']>['kind'], string> = {
  detail: 'Command',
  help: 'Help',
  error: 'Error',
};

const BORDER: Record<NonNullable<SessionState['overlay']>['kind'], string> = {
  detail: '#38bdf8',
  help: '#22c55e',
  error: '#f87171',
};

export class OverlayView {
  readonly output: BoxRenderable;
  #renderedKind: NonNullable<SessionState['overlay']>['kind'] | null = null;
  #renderedContent = '';

  constructor(private readonly renderer: CliRenderer) {
    this.output = new BoxRenderable(renderer, {
      id: 'overlay',
      width: '70%',
      height: '60%',
      position: 'absolute',
      left: '15%',
      top: '18%',
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      borderStyle: 'rounded',
      borderColor: '#38bdf8',
      backgroundColor: '#020617',
      zIndex: 10,
    });
  }

  render(state: SessionState): void {
    const overlay = state.overlay;
    if (overlay === null) {
      this.output.visible = false;
      return;
    }
    this.output.visible = true;
    if (this.#renderedKind === overlay.kind && this.#renderedContent === overlay.content) return;
    this.#renderedKind = overlay.kind;
    this.#renderedContent = overlay.content;
    this.output.borderColor = BORDER[overlay.kind];
    this.output.title = ` ${TITLE[overlay.kind]} `;
    this.#clear();
    this.output.add(
      new TextRenderable(this.renderer, {
        content: overlay.content,
        fg: overlay.kind === 'error' ? '#fecaca' : '#e2e8f0',
        width: '100%',
      }),
    );
    this.output.add(
      new TextRenderable(this.renderer, {
        content: 'Esc to close',
        fg: '#64748b',
        width: '100%',
      }),
    );
  }

  #clear(): void {
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
  }
}
