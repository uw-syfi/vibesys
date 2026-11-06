// Package clientcli is the verbs of vibesys-client, the terminal client of the orchestrator. It
// holds no run state and no product logic: every verb is calls on OrchestratorService through
// internal/client, and what it prints is a fold over the typed messages that come back.
//
// The package reads no clock, starts no process and handles no signal. Everything real arrives in
// Env from the binary's main: the terminal, the working directory, the clock, the way to reach
// the host, and a context that signals cancel.
package clientcli

import (
	"context"
	"io"
	"io/fs"
	"net"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
)

// Clock is the time the verbs read.
type Clock interface {
	Now() time.Time
	// After sends once, d from now.
	After(d time.Duration) <-chan time.Time
}

// Dial opens one connection to the service. It is called again after a connection breaks.
type Dial = func(ctx context.Context) (net.Conn, error)

// Screen is the terminal a person is at, for a view that takes keys. It is the only part of the
// terminal that is not plain reading of Stdin and writing of Stdout.
type Screen interface {
	// Raw makes the terminal deliver each key press at once and without echoing it, and returns the
	// function that puts the terminal back as it was. The function may be called more than once.
	Raw() (restore func(), err error)
	// Size is the width and height of the terminal in characters, or zeros when it cannot tell.
	Size() (width, height int)
	// Resizes receives once each time the terminal changes size.
	Resizes() <-chan struct{}
}

// Env is what a verb needs from the process it runs in.
type Env struct {
	// Version is the client's release version, sent in the handshake.
	Version string
	Stdin   io.Reader
	Stdout  io.Writer
	Stderr  io.Writer
	// Terminal says a person is at both ends: stdin and stdout are terminals. Without it nothing
	// prompts, and a verb that would hand over to an interactive view does not.
	Terminal bool
	// Screen is the terminal's keys and size, set when Terminal is. Without it a verb that would
	// take keys prints plain lines.
	Screen Screen
	// Color says output to the terminal may be styled with the 16 standard colours, dim and bold: a
	// terminal is at the other end, NO_COLOR is not set to anything and TERM is not "dumb". Main works
	// it out, the one place that reads the environment; it is false without a Terminal.
	Color bool
	// Dir is the working directory, absolute.
	Dir string
	// Home is the user's home directory, where a host started by this client looks for programs
	// (in .local/bin) unless its configuration names directories. Empty when there is none.
	Home string
	// FS is the file system rooted at "/", used to look for a repository's .git.
	FS fs.FS
	// Canonical returns the path with every symbolic link resolved, or the path unchanged when it
	// cannot be resolved (it no longer exists). The service names a run's repository in this form,
	// so a RUN left out is found by comparing canonical forms and not the text a person typed.
	Canonical func(path string) string
	// Zone is the time zone times are shown in.
	Zone  *time.Location
	Clock Clock
	// IDs mints the id of each command.
	IDs client.IDs
	// Connect finds the service for a --root flag value ("" for the default) and returns how to
	// reach it, starting the host when none serves. The Dial is its proof that a host can start here:
	// the program is found, and the configuration and state directory are ones a host would accept.
	// Otherwise it is an error, with *CannotStart for what an operator must put right, and nothing has
	// been started.
	Connect func(root string) (Dial, error)
	// Prove is Connect for a verb about to give up a working host: it also verifies the parts of the
	// release, which a start with no host to lose reports instead when a run needs them.
	Prove func(root string) (Dial, error)
	// HostBuild is the identity of the host this client would start, or empty when it cannot tell. It
	// is compared with what a host of no release says, and only then is it worked out.
	HostBuild func(root string) string
	// Locate is Connect without the start: the Dial it returns fails with ErrNoHost when no host
	// serves, and never starts one.
	Locate func(root string) (Dial, error)
	// Hosts finds and ends the process of a host that does not answer.
	Hosts HostProcesses
	// Configs is the host's configuration in each root.
	Configs HostConfigs
}
