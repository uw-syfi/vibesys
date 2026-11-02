// Package sim holds the whole-daemon simulation. It has no product code: its test runs the real
// app, server and client together over Fakes, inside a testing/synctest bubble, and every choice
// that would otherwise be left to the Go scheduler comes from a seed.
//
// One goroutine, the driver, decides everything. It waits until every other goroutine in the bubble
// is blocked, then picks one thing that may happen next: one store or supervisor call may go on
// (and how it goes: as it should, failing, or with the daemon dying), a timer fires, a command
// moves on, or a client calls. So a failing seed replays exactly, and after each step the driver
// checks what must always hold.
//
// The test takes three variables. VIBESYS_SIM_SEEDS is how many seeds to run from the first, which
// the default keeps to what check.sh can afford under the race detector, and hunting for a defect
// takes more. VIBESYS_SIM_SEED runs one seed, and VIBESYS_SIM_TRACE prints what a failing seed did.
//
// No seed is pinned. A seed names a schedule only for the simulator it ran on, so any change here
// gives it another meaning: a defect is pinned by a named test of its own.
package sim
