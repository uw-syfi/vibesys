// Package reading turns what one benchmark run wrote into the value of the scored metric.
//
// A benchmark reports in one of two ways. It writes a JSON file and the task names the field that
// holds the metric (JSONField), or it speaks the evaluator result protocol, version 2, and writes
// a stream of JSON records (Protocol2). Each record is the generated evaluatorv1.ResultRecord
// (proto/vibesys/evaluator/v1/records.proto), which is the one definition of what a record may
// hold; the prose is sdk/vs-evaluator/PROTOCOL.md. This reader passes the corpus in
// sdk/vs-evaluator/fixtures: every invalid stream is rejected with the reason code the corpus
// records for it. What stays here is what needs the whole stream: the hello comes first, one
// outcome, every value names a declared metric.
//
// Both readers are pure functions of the bytes. They bound nothing but the size of one record
// line, so the caller bounds how much of a file it reads (MaxFileBytes), and a fuzzer can feed
// them anything. The output of a benchmark is untrusted: a reader never panics, never accepts a
// non-finite number, and says in its error which record or key it rejected.
package reading
