/**
 * Plain folds and views over the app's data (`neutral` runtime). Imports nothing outside this
 * folder at run time and never `effect` (`desktop-state-*` rules in `.dependency-cruiser.mjs`), so
 * a fold is a function from data to data that any test calls directly.
 */
export {};
