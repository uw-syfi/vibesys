// Package sim holds the whole-daemon simulation. It has no product code: its test runs the real
// app, server and client together over Fakes, inside a testing/synctest bubble, and every choice
// that would otherwise be left to the Go scheduler comes from a seed.
//
// One goroutine, the driver, decides everything. It waits until every other goroutine in the bubble
// is blocked, then picks one thing that may happen next: one store or supervisor call may go on
// (and how it goes: as it should, failing, or with the daemon dying), a timer fires, a command
// moves on, or a client calls. So a failing seed replays exactly, and after each step the driver
// checks what must always hold.
package sim
