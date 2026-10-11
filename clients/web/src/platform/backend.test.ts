import {expect, test} from 'bun:test';
import {bootstrapGateway} from './backend.js';

test('uses a credentialed no-store bootstrap and rejects a failed exchange', async () => {
  const calls: Array<{url: string; init: RequestInit | undefined}> = [];
  const successful = async (url: string | URL | Request, init?: RequestInit): Promise<Response> => {
    calls.push({url: String(url), init});
    return new Response('', {
      status: 200,
      headers: {'X-VibeSys-Browser-Session': 'browser-session'},
    });
  };

  expect(await bootstrapGateway('http://127.0.0.1:8765/?token=secret', successful)).toBe(
    'browser-session',
  );
  expect(calls).toEqual([
    {
      url: 'http://127.0.0.1:8765/?token=secret',
      init: {cache: 'no-store', credentials: 'include', mode: 'cors'},
    },
  ]);

  const rejected = async (): Promise<Response> => new Response('', {status: 403});
  await expect(bootstrapGateway('http://127.0.0.1:8765/?token=old', rejected)).rejects.toThrow(
    'HTTP 403',
  );
});
