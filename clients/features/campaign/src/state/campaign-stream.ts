import type {CampaignFrame} from './campaign-frames.js';

export type FrameListener = (frame: CampaignFrame) => void;
export type StreamErrorListener = (error: Error) => void;

/** Parsed campaign frames. The creator of a subscription releases it through its disposer. */
export interface CampaignStream {
  subscribe(onFrame: FrameListener, onError: StreamErrorListener): () => void;
}
