import type {RunPulse} from '../model.js';

// Scaffold; task 9 announces status changes and new rounds.
export function LiveRegion(_props: RunPulse) {
  return <p className="sr-only" role="status" />;
}
