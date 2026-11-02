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
//	emit TEXT...       write the text, with no newline, to the output file the command was given
//	hold GATE          wait until the gate is released; a cancel ends the run, cancelled
//	exit N             end the run, exited with code N
//	timeout            end the run, timed out
//	service-not-ready  end the run, the service never became ready
//	service-exited     end the run, the service exited early
//	unknown            end the run in a way the environment cannot say: Run returns core.Unobserved
//	fail TEXT...       as the first step, the command cannot be started: Run returns core.NotStarted
//	                   with an unavailable failure, unless an earlier process of the run started,
//	                   which makes it core.Unobserved; later in a script, it ends the run unobserved
//
// The output file is the last argument of the command when it is an absolute path that follows a
// flag, such as `--vs-output /scratch/sample-0.out`: the fake takes both off the script, as a
// benchmark takes them off its command line, and `emit` writes to the path (see WithFiles).
//
// A script that runs out of steps exits with code 0. A step it does not know exits with code 127,
// as a shell does for a command it cannot find.
package fakeenv
