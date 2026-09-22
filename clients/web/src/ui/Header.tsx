import type {HeaderModel} from '../model.js';

export interface HeaderProps {
  model: HeaderModel;
  /** "Pause failed: …" or "Resume failed: …", shown next to the control until the next command. */
  error: string | null;
  onControl: () => void;
}

// Scaffold; task 5 renders the header.
export function Header(_props: HeaderProps) {
  return <header className="hdr" />;
}
