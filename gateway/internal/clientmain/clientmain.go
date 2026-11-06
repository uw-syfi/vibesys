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
	"runtime"
	"strconv"
	"syscall"
	"time"

	"github.com/mattn/go-isatty"

	"github.com/uw-syfi/vibesys/gateway/internal/buildinfo"
	"github.com/uw-syfi/vibesys/gateway/internal/connect"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

const (
	// hostProgram is the executable that serves the host, looked up on PATH when this program is
	// in no release.
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
		Version: buildinfo.Version, Stdin: os.Stdin, Stdout: os.Stdout, Stderr: os.Stderr,
		Terminal: isatty.IsTerminal(os.Stdin.Fd()) && isatty.IsTerminal(os.Stdout.Fd()),
		Dir:      dir, Home: homeDir(), FS: os.DirFS("/"), Canonical: canonical, Zone: time.Local, Clock: systemClock{}, IDs: randomIDs{},
		Connect: connector, Prove: prover, HostBuild: hostBuild, Locate: locator, Hosts: hostProcesses{}, Configs: hostConfigs{},
	})
}

// homeDir is the user's home directory, or empty when there is none.
func homeDir() string {
	home, _ := os.UserHomeDir()
	return home
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

// connector says how to reach the host for a --root value, starting it when none serves, once it has
// shown that a host could start there.
func connector(root string) (clientcli.Dial, error) {
	directory, err := hostRoot(root)
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
	home, _ := os.UserHomeDir()
	if err := checkStart(directory, home, os.Getenv); err != nil {
		return nil, err
	}
	launcher := connect.Process{
		Binary: host, Args: []string{"serve", "--root", directory, "--start-report-fd", strconv.Itoa(connect.ReportFD)},
		// The host's whole environment is its own to decide; this is what it needs to find its home.
		Env: hostconfig.Environ(nil, home, os.Getenv), Dir: directory, Log: lifecycle.LogPath(directory),
	}
	socket := lifecycle.SocketPath(directory)
	return func(ctx context.Context) (net.Conn, error) {
		ctx, cancel := context.WithTimeout(ctx, startup)
		defer cancel()
		connection, err := connect.Dial(ctx, socket, retiredNotice{launcher}, connect.HoldingOf(directory))
		return connection, startFailure(err)
	}, nil
}

// prover is connector for a verb about to give up a working host: the parts of the release this
// program is installed in must be whole too. A program in no release has no parts to verify.
func prover(root string) (clientcli.Dial, error) {
	dial, err := connector(root)
	if err != nil {
		return nil, err
	}
	self, err := os.Executable()
	if err != nil {
		return nil, err
	}
	rel, err := release.FinderFor(self)()
	if err != nil {
		var notInstalled *release.NotInstalledError
		if errors.As(err, &notInstalled) {
			return dial, nil
		}
		return nil, &clientcli.CannotStart{Problem: err.Error()}
	}
	if err := rel.Verify(release.Installed); err != nil {
		return nil, &clientcli.CannotStart{Problem: err.Error()}
	}
	if err := rel.CheckPlatform(runtime.GOOS, runtime.GOARCH); err != nil {
		return nil, &clientcli.CannotStart{Problem: err.Error()}
	}
	return dial, nil
}

// hostBuild is the identity of the host this program would start, or empty when it cannot tell.
func hostBuild(string) string {
	self, err := os.Executable()
	if err != nil {
		return ""
	}
	host, err := hostBinary(self, exec.LookPath)
	if err != nil {
		return ""
	}
	id, err := buildinfo.ID(host)
	if err != nil {
		return ""
	}
	return id
}

// retiredNotice names the settings this environment still has that no host reads any more, each
// time a start is about to ignore them.
type retiredNotice struct{ connect.Process }

func (n retiredNotice) Launch(ctx context.Context) (*pb.HostStartReport, error) {
	for _, setting := range hostconfig.Retired {
		if os.Getenv(setting.Name) != "" {
			fmt.Fprintf(os.Stderr, "note: %s is no longer read; use `%s host configure %s`\n", setting.Name, clientcli.Program, setting.Flag)
		}
	}
	return n.Process.Launch(ctx)
}

// startFailure gives a failed start the type the verbs read their exit code from.
func startFailure(err error) error {
	var refused *connect.RefusedError
	var ended *connect.EndedError
	switch {
	case errors.As(err, &refused):
		return &clientcli.StartRefused{Detail: refused.Why.GetDetail()}
	case errors.As(err, &ended):
		return &clientcli.StartRefused{Detail: ended.Error()}
	case errors.Is(err, context.DeadlineExceeded):
		return &clientcli.StartTimedOut{Err: err}
	}
	return err
}

// hostBinary finds the vibesys-host to start: the host program of the release this program is
// installed in (the client is usually linked into ~/.local/bin from its release directory, and the
// release's manifest follows the link), else the one on PATH, which is how a development build
// finds it. A release whose host is damaged is an error naming the file, never another build.
func hostBinary(self string, lookPath func(string) (string, error)) (string, error) {
	rel, err := release.FinderFor(self)()
	var notInstalled *release.NotInstalledError
	switch {
	case err == nil:
		return rel.HostProgram()
	case !errors.As(err, &notInstalled):
		return "", err
	}
	if found, err := lookPath(hostProgram); err == nil {
		return found, nil
	}
	return "", errors.New("no " + hostProgram + ": " + err.Error() + ", and none on PATH")
}
