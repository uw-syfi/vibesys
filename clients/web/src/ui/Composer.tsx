import type {PendingSteer} from '../model.js';

export interface ComposerProps {
  pending: readonly PendingSteer[];
  /** Disconnected: typing stays possible, sending does not, and focus is kept. */
  disabled: boolean;
  /** "Steer failed: …" until the next command. */
  error: string | null;
  onSend: (text: string) => Promise<boolean>;
}

// Scaffold; task 8 renders the composer dock.
export function Composer(_props: ComposerProps) {
  return <div className="dock" />;
}
