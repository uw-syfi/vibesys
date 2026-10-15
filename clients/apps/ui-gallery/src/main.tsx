import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {applyTheme} from '@vibesys/ui';
import {DEFAULT_THEMES} from '@vibesys/ui/tokens';
import {createRoot} from 'react-dom/client';
import {Gallery, themeFromQuery} from './gallery.js';

const theme = themeFromQuery(window.location.search) ?? DEFAULT_THEMES.light;
applyTheme(theme);
const root = document.getElementById('root');
if (root === null) throw new Error('index.html has no #root');
createRoot(root).render(<Gallery theme={theme} />);
