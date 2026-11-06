// Package clientmain composes vibesys-client: it gives the verbs in orchestrator/clientcli the real
// terminal, signals, clock and working directory, and the way to reach, and start, the host.
// Nothing here decides what a verb does.
package clientmain

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/mattn/go-isatty"

	"github.com/uw-syfi/vibesys/gateway/internal/connect"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	schema "github.com/uw-syfi/vibesys/gateway/schemahash"
	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
)

const (
	version = "0.2.0"
	// hostProgram is the executable that serves the host; it is found beside this program.
	hostProgram = "vibesys-host"
	// startup bounds how long a connection waits for a host it has started.
	startup = 10 * time.Second
)

// Main runs the client command and returns its process exit status.
func Main(args []string) int {
	// A closed pipe (`| head`) is a normal end of output, seen as a write error.
	signal.Ignore(syscall.SIGPIPE)
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	dir, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s: %v\n", clientcli.Program, err)
		return clientcli.ExitFailed
	}
	return clientcli.Main(ctx, args, clientcli.Env{
		Version: version, Stdin: os.Stdin, Stdout: os.Stdout, Stderr: os.Stderr,
		Terminal: isatty.IsTerminal(os.Stdin.Fd()) && isatty.IsTerminal(os.Stdout.Fd()),
		Dir:      dir, FS: os.DirFS("/"), Canonical: canonical, Zone: time.Local, Clock: systemClock{}, IDs: randomIDs{},
		Connect: connector,
	})
}

// canonical resolves the symbolic links of a path, and leaves it as it is when it cannot.
func canonical(path string) string {
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		return resolved
	}
	return path
}

type systemClock struct{}

func (systemClock) Now() time.Time                         { return time.Now() }
func (systemClock) After(d time.Duration) <-chan time.Time { return time.After(d) }

// randomIDs mints command ids that no other command line shares.
type randomIDs struct{}

func (randomIDs) NewCommandID() string {
	var b [12]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err) // the system has no randomness; no id could be told apart.
	}
	return "client-" + hex.EncodeToString(b[:])
}

// connector says how to reach the host for a --root value, starting it when none serves.
func connector(root string) (clientcli.Dial, error) {
	home, _ := os.UserHomeDir()
	directory, err := gatewayroot.For(root, os.Getenv, home)
	if err != nil {
		return nil, err
	}
	self, err := os.Executable()
	if err != nil {
		return nil, err
	}
	host, err := hostBinary(self, exec.LookPath)
	if err != nil {
		return nil, err
	}
	socket := lifecycle.SocketPath(directory, schema.APISchemaHash)
	return func(ctx context.Context) (net.Conn, error) {
		ctx, cancel := context.WithTimeout(ctx, startup)
		defer cancel()
		return connect.Dial(ctx, socket, host, []string{"serve", "--root", directory}, os.Stderr)
	}, nil
}

// hostBinary finds the vibesys-host to start: the one beside this program, with symlinks followed
// (the client is usually linked into ~/.local/bin from its release directory), else the one on
// PATH, which is how a development build finds it.
func hostBinary(self string, lookPath func(string) (string, error)) (string, error) {
	var beside string
	if real, err := filepath.EvalSymlinks(self); err == nil {
		beside = filepath.Join(filepath.Dir(real), hostProgram)
		if info, err := os.Stat(beside); err == nil && info.Mode().IsRegular() {
			return beside, nil
		}
	}
	if found, err := lookPath(hostProgram); err == nil {
		return found, nil
	}
	return "", errors.New("no " + hostProgram + " beside " + self + " or on PATH: install it next to " + clientcli.Program)
}
