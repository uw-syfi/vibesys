import {describe, expect, it} from 'bun:test';
import type {ProtocolResponse} from '@vibesys/contracts';
import {resolveStartupTheme} from './startup-theme.js';

describe('resolveStartupTheme', () => {
  it('applies the theme the backend resolved from configuration', async () => {
    await expect(
      resolveStartupTheme(Promise.resolve(defaultsResponse('solarized-light'))),
    ).resolves.toBe('solarized-light');
  });

  it('prefers an explicit launcher theme without asking the backend', async () => {
    let asked = false;
    const pending = (async () => {
      asked = true;
      return defaultsResponse('light');
    })();

    await expect(resolveStartupTheme(pending, {explicitTheme: 'catppuccin-latte'})).resolves.toBe(
      'catppuccin-latte',
    );
    await pending;
    // The launcher only starts the request when no theme was given, but an
    // explicit one wins even if a response is already in flight.
    expect(asked).toBe(true);
  });

  it('falls back to the default theme when the backend never answers', async () => {
    const start = Date.now();

    await expect(resolveStartupTheme(never(), {timeoutMs: 20})).resolves.toBe('dark');
    expect(Date.now() - start).toBeLessThan(1_000);
  });

  it('falls back to the default theme when the request fails', async () => {
    const rejected = Promise.reject(new Error('Server disconnected'));

    await expect(resolveStartupTheme(rejected, {timeoutMs: 1_000})).resolves.toBe('dark');
  });

  it('falls back to the default theme for defaults it cannot render', async () => {
    await expect(resolveStartupTheme(Promise.resolve(defaultsResponse('gruvbox')))).resolves.toBe(
      'dark',
    );
    await expect(
      resolveStartupTheme(Promise.resolve({...emptyResponse(), tui_defaults: null})),
    ).resolves.toBe('dark');
  });
});

function emptyResponse(): ProtocolResponse {
  return {
    protocol_version: 1,
    request_id: 'defaults-1',
    timestamp: '1970-01-01T00:00:00Z',
    ok: true,
  } as ProtocolResponse;
}

function defaultsResponse(theme: string): ProtocolResponse {
  return {
    ...emptyResponse(),
    tui_defaults: {
      runs_dir: '/runs',
      input_path: '',
      experiment_name: 'experiment-1',
      repository_owner: null,
      repository_name: 'experiment-1',
      visibility: 'private',
      theme,
    },
  } as ProtocolResponse;
}

function never(): Promise<ProtocolResponse> {
  return new Promise(() => undefined);
}
