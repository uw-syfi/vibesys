import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {applyTheme} from '@vibesys/ui';
import {DEFAULT_THEMES} from '@vibesys/ui/tokens';
import {createRoot} from 'react-dom/client';
import {RunLayoutDesign, zoomFromQuery} from './design/run-layout.js';
import {Gallery, themeFromQuery} from './gallery.js';

const theme = themeFromQuery(window.location.search) ?? DEFAULT_THEMES.light;
applyTheme(theme);
const root = document.getElementById('root');
if (root === null) throw new Error('index.html has no #root');
const design = new URLSearchParams(window.location.search).get('design');
if (design === 'run-layout') {
  const {zoom, flags} = zoomFromQuery(window.location.search);
  createRoot(root).render(<RunLayoutDesign initial={zoom} flags={flags} />);
} else {
  createRoot(root).render(<Gallery theme={theme} />);
}
