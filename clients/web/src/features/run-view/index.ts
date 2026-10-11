// The run view: the run's transcript, rounds, and status, the connectivity banners, and the live
// session that feeds them. The only public surface of this slice.
export {
  createRunStore,
  type RunStore,
  WebSession,
  type WebSessionOptions,
} from './effects/session.js';
export {
  CONTROLS_BANNER_COPY,
  EMPTY_TRANSCRIPT_COPY,
  LIVE_SESSION_HEADER_COPY,
  PAGE_CONNECTION_BANNER_COPY,
  STREAM_BANNER_COPY,
} from './state/banners.js';
export {RunPage} from './ui/run-page.js';
