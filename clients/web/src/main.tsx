import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {createDemoApp, createLiveApp} from './App.js';
import {WebSession} from './session.js';
import './styles.css';

const root = document.querySelector('#root');
if (root === null) throw new Error('Web viewer root is missing');
const isGatewayPage = new URL(window.location.href).searchParams.has('token');
const session = isGatewayPage ? new WebSession() : null;
createRoot(root).render(
  <StrictMode>{session === null ? createDemoApp() : createLiveApp(session)}</StrictMode>,
);
