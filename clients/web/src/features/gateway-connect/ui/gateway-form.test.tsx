import {describe, expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {GatewayConnectState} from '../state/gateway-connect.js';
import {GatewayForm} from './gateway-form.js';

const PAGE = 'http://127.0.0.1:5173/';

describe('GatewayForm connection announcements', () => {
  for (const message of [
    'Gateway connection timed out after 10000 ms',
    'Gateway connection cancelled. Retry when ready.',
  ]) {
    test(`announces ${message}`, () => {
      const state: GatewayConnectState = {
        kind: 'editing',
        page: PAGE,
        url: '',
        error: message,
        retryTarget: null,
        next: 1,
      };

      const html = renderToStaticMarkup(<GatewayForm state={state} send={() => undefined} />);

      expect(html).toContain('class="gateway-connect-error"');
      expect(html).toContain('role="status"');
      expect(html).toContain('aria-live="polite"');
      expect(html).toContain('aria-atomic="true"');
      expect(html).toContain(message);
    });
  }
});
