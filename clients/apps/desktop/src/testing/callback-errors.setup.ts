import {afterEach} from 'vitest';
import {assertNoCallbackErrors} from './callback-errors.js';

afterEach(assertNoCallbackErrors);
