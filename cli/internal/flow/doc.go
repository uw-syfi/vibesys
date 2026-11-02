// Package flow is what a check job and a measure job do, as a pure step function.
//
// A job's work is a short sequence: check out the commit, build, check, sample the machine, run
// the benchmark once per sample and read what each run wrote. Step takes the state and one event,
// the answer to the previous request, and returns the new state and the next request. It reads
// no clock, starts no goroutine and touches no file: the shell that carries out requests
// (package flowrun) stamps events with the time it saw, runs commands through an Environment and
// reads files through hostfs. That split makes the flow checkable by construction. A test feeds
// Step any event sequence, with a failure or a cancel at any step, and the invariants hold in
// every one of them:
//
//   - a job that passed took every step: the build and the check exited 0 and every sample
//     came from a benchmark run that exited 0 and wrote a result the reader accepted;
//   - a job that failed or was cancelled carries no findings, so a cancelled measure saves
//     nothing and a result never lacks its samples or its check;
//   - once a verdict is reached the machine asks for nothing more.
//
// Plan is what the daemon decides before the job starts, and Prepare is the pure rule that makes
// it from the job, the task and the results already saved.
package flow
