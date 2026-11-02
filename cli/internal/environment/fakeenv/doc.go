// Package fakeenv is a fake environment.Environment that runs scripts and not commands, so a test
// drives the whole job lifecycle (a job that exits, hangs, times out, is cancelled or cannot be
// attempted) without a shell, a clock or a container.
//
// It is a real implementation of the interface, not a stub with canned answers: it ends every
// run on every path (an exit, a cancelled context, a script that fails) and reports the outcomes
// the contract names. It lives in a support package because production never uses it; the daemon
// and supervisor binaries the end-to-end tests start are built with it injected.
//
// A command is a script: steps separated by ";" arguments. The steps are
//
//	print TEXT...      write the text and a newline to the output
//	hold GATE          wait until the gate is released; a cancel ends the run, cancelled
//	exit N             end the run, exited with code N
//	timeout            end the run, timed out
//	service-not-ready  end the run, the service never became ready
//	service-exited     end the run, the service exited early
//	unknown            end the run, outcome unknown
//	fail TEXT...       the run cannot be attempted: Run returns an unavailable failure
//
// A script that runs out of steps exits with code 0. A step it does not know exits with code 127,
// as a shell does for a command it cannot find.
package fakeenv
