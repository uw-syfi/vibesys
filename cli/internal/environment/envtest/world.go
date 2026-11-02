package envtest

import "context"

// World is the place the suite's programs run in, seen from the test. It renders a Program into
// the command an environment is given, and it is how the test releases, observes and checks the
// processes that command starts.
//
// The test's side of every method is non-blocking or bounded by its context, and none polls: a
// wait ends when the process it waits for produces the event.
type World interface {
	// Argv returns the command that makes the environment run the program. It prepares whatever
	// the program names, so it is called once per program and before the program runs.
	Argv(p Program) []string

	// Missing returns a command that does not exist, which an environment cannot start.
	Missing() []string

	// Release lets every process that waits at the point go on, now and later.
	Release(point string)

	// Reached waits until a process has marked the point, and reports false if ctx ends first.
	// Once a point has been marked it stays reached.
	Reached(ctx context.Context, point string) bool

	// Marked reports whether a process has marked the point, without waiting.
	Marked(point string) bool

	// SetFlag sets the flag that UnlessFlag reads.
	SetFlag(name string)

	// Gone waits until the process that ran Alive(name) has ended, and reports false if ctx ends
	// first. It waits for the process to reach Alive first.
	Gone(ctx context.Context, name string) bool
}
