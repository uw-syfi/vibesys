import {describe, expect, it} from 'vitest';
import {type RuntimeRenderer, runTuiSession} from './runtime.js';

describe('TUI runtime lifecycle', () => {
  it('cleans up the renderer, app, and controller when startup fails', async () => {
    const calls: string[] = [];
    const renderer: RuntimeRenderer = {
      start: () => calls.push('renderer.start'),
      destroy: () => calls.push('renderer.destroy'),
      once: () => undefined,
    };
    const controller = {
      start: async () => {
        calls.push('controller.start');
        throw new Error('subscription failed');
      },
      stop: async () => {
        calls.push('controller.stop');
      },
    };
    const app = {destroy: () => calls.push('app.destroy')};

    await expect(runTuiSession(renderer, controller, app)).rejects.toThrow('subscription failed');
    expect(calls).toEqual([
      'renderer.start',
      'controller.start',
      'renderer.destroy',
      'app.destroy',
      'controller.stop',
    ]);
  });
});
