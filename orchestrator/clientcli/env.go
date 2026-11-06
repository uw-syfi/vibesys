// Package clientcli is the verbs of vibesys-client, the terminal client of the orchestrator. It
// holds no run state and no product logic: every verb is calls on OrchestratorService through
// internal/client, and what it prints is a fold over the typed messages that come back.
//
// The package reads no clock, starts no process and handles no signal. Everything real arrives in
// Env from the binary's main: the terminal, the working directory, the clock, the way to reach
// the host, and a context that signals cancel.
package clientcli

import (
	"io"
	"io/fs"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
)

// Clock is the time the verbs read.
type Clock interface {
	Now() time.Time
	// After sends once, d from now.
	After(d time.Duration) <-chan time.Time
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
	// Dir is the working directory, absolute.
	Dir string
	// FS is the file system rooted at "/", used to look for a repository's .git.
	FS fs.FS
	// Zone is the time zone times are shown in.
	Zone  *time.Location
	Clock Clock
	// IDs mints the id of each command.
	IDs client.IDs
	// Connect finds the service for a --root flag value ("" for the default) and returns how to
	// reach it, starting the host when none serves.
	Connect func(root string) (client.Dialer, error)
}
