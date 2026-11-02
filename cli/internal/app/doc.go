// Package app is the daemon's job flows: it admits submissions, runs the lifecycle machine, and
// carries out what the machine asks for.
//
// The split follows the functional core. lifecycle.Step decides, from the events it is told of,
// which job runs next, what a finished job's end is and what a restart finds; App is the shell
// that reads and writes the stores, starts and follows supervisors, and answers every request
// the machine makes with the event that says how it went. App decides nothing about a job: when
// a rule is missing it belongs in internal/lifecycle or internal/core.
//
// Durable intent comes before I/O. A job is queued in its store before the machine hears of it,
// and recorded as running before its command starts, so a crash at any point leaves a state
// that the next start reads from the stores and the supervisors alone (App.Start). There is no
// other memory.
package app
