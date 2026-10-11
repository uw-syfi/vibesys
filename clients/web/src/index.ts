// `@vibesys/web`'s public surface: the composed run page and the session that drives it. The
// backend client's runtime values stay behind `platform/backend.ts`; they are not re-exported.
export {createDemoApp, createLiveApp, type DemoAppOptions} from './app/compose.js';
export type {WebSessionState, WebSessionStatus} from './entities/connection.js';
export type {GatewayConnection} from './entities/gateway.js';
export {WebSession, type WebSessionOptions} from './features/run-view/index.js';
export type {BrowserLifecycle} from './platform/lifecycle.js';
