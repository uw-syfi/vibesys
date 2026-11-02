// Package reading turns what one benchmark run wrote into the value of the scored metric.
//
// A benchmark reports in one of two ways. It writes a JSON file and the task names the field that
// holds the metric (JSONField), or it speaks the evaluator result protocol, version 2, and writes
// a stream of JSON records (Protocol2). The protocol is defined in sdk/vs-evaluator/PROTOCOL.md,
// and this reader passes the corpus in sdk/vs-evaluator/fixtures: every invalid stream is
// rejected with the reason code the corpus records for it.
//
// Both readers are pure functions of the bytes. They bound nothing but the size of one record
// line, so the caller bounds how much of a file it reads (MaxFileBytes), and a fuzzer can feed
// them anything. The output of a benchmark is untrusted: a reader never panics, never accepts a
// non-finite number, and says in its error which record or key it rejected.
package reading
