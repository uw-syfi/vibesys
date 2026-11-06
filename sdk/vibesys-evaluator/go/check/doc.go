// Package check is vibesys-evaluator's model-based accuracy checker: it decides whether a
// captured history of a candidate's responses is one the task's oracle allows.
//
// The task writes the oracle as one or more Specs, in Go. A Spec projects the history's calls
// onto keyed Pieces (Project) and models one key's state (Init, Step, optionally Fault). The
// check splits the pieces into partitions by key, samples reads, and searches each partition for
// an order of its pieces that respects real time and that the model accepts (porcupine's
// linearizability search), under time and memory budgets. The verdict is PASS, FAIL (with the
// first violation explained and an HTML timeline) or UNKNOWN (something could not be decided;
// never a pass), with what was covered.
//
// Main is the oracle's command line: it checks a history directory the accuracy capture wrote
// (OpenHistory), and its import command makes one of older loadgen phase directories
// (WriteHistory). Register, Window, Counter and PMap are building blocks for model states.
//
// The authoring guide is sdk/vibesys-evaluator/docs/accuracy-checks.md; the history directory's
// format is in sdk/vibesys-evaluator/docs/spec.md ("Accuracy history").
package check
