import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {createDemoApp} from './App.js';
import './styles.css';

const root = document.querySelector('#root');
if (root === null) throw new Error('Web viewer root is missing');
createRoot(root).render(<StrictMode>{createDemoApp()}</StrictMode>);
