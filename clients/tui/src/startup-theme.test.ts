import {describe, expect, it} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {
  PROTOCOL_VERSION,
  type ProtocolResponse,
  RepositoryVisibility,
  ResponseSchema,
  TuiTheme,
} from '@vibesys/backend-client';
import {resolveStartupTheme} from './startup-theme.js';

describe('resolveStartupTheme', () => {
  it('applies the theme the backend resolved from configuration', async () => {
    await expect(
      resolveStartupTheme(Promise.resolve(defaultsResponse(TuiTheme.SOLARIZED_LIGHT))),
    ).resolves.toBe('solarized-light');
  });

  it('prefers an explicit launcher theme without asking the backend', async () => {
    let asked = false;
    const pending = (async () => {
      asked = true;
      return defaultsResponse(TuiTheme.LIGHT);
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
    await expect(
      resolveStartupTheme(Promise.resolve(defaultsResponse(99 as TuiTheme))),
    ).resolves.toBe('dark');
    await expect(resolveStartupTheme(Promise.resolve(emptyResponse()))).resolves.toBe('dark');
  });
});

function emptyResponse(): ProtocolResponse {
  return create(ResponseSchema, {
    protocolVersion: PROTOCOL_VERSION,
    requestId: 'defaults-1',
    ok: true,
  });
}

function defaultsResponse(theme: TuiTheme): ProtocolResponse {
  return create(ResponseSchema, {
    protocolVersion: PROTOCOL_VERSION,
    requestId: 'defaults-1',
    ok: true,
    tuiDefaults: {
      runsDir: '/runs',
      inputPath: '',
      experimentName: 'experiment-1',
      repositoryName: 'experiment-1',
      visibility: RepositoryVisibility.PRIVATE,
      theme,
    },
  });
}

function never(): Promise<ProtocolResponse> {
  return new Promise(() => undefined);
}
