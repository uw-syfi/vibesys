/**
 * `structuredClone` is a global of every runtime this entry runs in (browsers, Node, Bun), but
 * the ES library does not declare it; the DOM and Node libraries both do. Declaring the one
 * function here keeps the entry runtime neutral rather than borrowing either library.
 */
declare function structuredClone<T>(value: T): T;
