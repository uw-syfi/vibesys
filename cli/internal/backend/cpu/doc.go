// Package cpu is the backend.Backend of the CPU compute stack: the facts about the machine a job
// runs on and samples of its load, which results record.
//
// A Backend is pure arithmetic over a System, the one place the machine is read. The shipped System
// reads /proc on Linux and the system control interface on macOS; a test supplies a FakeSystem
// whose readings it scripts. The parsers of the /proc files are pure functions of their text.
//
// Load is reported and never acted on. CPUBusyFraction covers the time between two calls of Load
// on one Backend, because the machine only counts CPU time as it passes; the first call covers
// the time since boot. A caller that wants the load of a stretch of work calls Load at its start
// and again at its end.
package cpu
