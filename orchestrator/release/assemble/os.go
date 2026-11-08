package assemble

import (
	"bytes"
	"context"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// DirSink is a Sink that writes under a folder on this machine.
type DirSink struct{ Root string }

var _ Sink = DirSink{}

// MkdirAll implements Sink.
func (s DirSink) MkdirAll(dir string) error {
	return os.MkdirAll(filepath.Join(s.Root, filepath.FromSlash(dir)), 0o755)
}

// WriteFile implements Sink.
func (s DirSink) WriteFile(name string, data []byte, perm fs.FileMode) error {
	return os.WriteFile(filepath.Join(s.Root, filepath.FromSlash(name)), data, perm)
}

// Runner runs a program with exactly the environment it is given, and returns what it wrote. The error
// is the program's status.
type Runner interface {
	Run(ctx context.Context, program string, args, env []string) (stdout, stderr []byte, err error)
}

// ExecRunner is the real program, as a Runner.
type ExecRunner struct{}

var _ Runner = ExecRunner{}

// Run implements Runner.
func (ExecRunner) Run(ctx context.Context, program string, args, env []string) ([]byte, []byte, error) {
	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.Env, cmd.Stdout, cmd.Stderr = env, &stdout, &stderr
	err := cmd.Run()
	return stdout.Bytes(), stderr.Bytes(), err
}

// CLICardReader reads a card by running `vibesys-cli task card --json --folder`. The CLI's daemon is
// the one reader of task files, so this is the reader a run would use.
//
// The daemon it starts is its own, and shares nothing with the daemon of the person who builds. A
// user has one daemon on a machine, which holds the machine lock whatever its runtime directory, and a
// program with no runtime directory of its own reaches the user's default one: a card read that took
// either would fail the build while the user's daemon runs, or read through that daemon and then stop
// it. So the reader's environment is made here, from State alone and never from the builder's: a
// sandbox daemon (which takes no machine lock) with State as its home, its temporary files and its
// runtime directory. Stop ends it.
type CLICardReader struct {
	Program string
	// State is an existing folder the reader owns while it is used, which holds all the daemon's state
	// and none of the builder's. It is short enough for a socket's path, as a folder made under /tmp is.
	State string
	// Runner runs the program. Nil is ExecRunner.
	Runner Runner
}

var _ CardReader = CLICardReader{}

// Environ is the whole environment the program runs in: nothing of the builder's reaches it.
func (r CLICardReader) Environ() []string {
	return []string{
		"HOME=" + r.State,
		"TMPDIR=" + r.State,
		"XDG_RUNTIME_DIR=" + filepath.Join(r.State, "run"),
		// The name and the value are the CLI's (production.EnvSandbox in vibesys-cli): a sandbox daemon
		// holds no machine lock and is told apart from the one a user's work depends on.
		"VIBESYS_CLI_SANDBOX=1",
	}
}

// Card implements CardReader.
func (r CLICardReader) Card(ctx context.Context, folder, task string) (*cliv1.TaskCard, error) {
	out, err := r.run(ctx, "task", "card", "--json", "--folder", folder, task)
	if err != nil {
		return nil, err
	}
	var card cliv1.TaskCard
	if err := (protojson.UnmarshalOptions{}).Unmarshal(out, &card); err != nil {
		return nil, fmt.Errorf("the card the CLI printed is not a TaskCard: %w", err)
	}
	return &card, nil
}

// Stop stops the daemon the card reads started, which is the reader's own.
func (r CLICardReader) Stop(ctx context.Context) error {
	_, err := r.run(ctx, "daemon", "stop")
	return err
}

func (r CLICardReader) run(ctx context.Context, args ...string) ([]byte, error) {
	runner := r.Runner
	if runner == nil {
		runner = ExecRunner{}
	}
	stdout, stderr, err := runner.Run(ctx, r.Program, args, r.Environ())
	if err != nil {
		return nil, fmt.Errorf("%s %s: %w: %s", filepath.Base(r.Program), strings.Join(args[:2], " "), err, strings.TrimSpace(string(stderr)))
	}
	return stdout, nil
}

// GitExternals fetches an external example's own files from the repository the checkout pins as a
// submodule: the URL from .gitmodules and the commit from the gitlink, at the checkout's HEAD.
// Scratch is an empty folder the fetches go in.
type GitExternals struct {
	Repo    string
	Scratch string
}

var _ Externals = GitExternals{}

// Fetch implements Externals. It fetches the one commit, without history or file contents but the
// example's .vibesys folder.
func (g GitExternals) Fetch(ctx context.Context, e Entry) (fs.FS, Pin, error) {
	urlOut, err := git(ctx, g.Repo, "config", "--file", ".gitmodules", "--get-regexp", `^submodule\..*\.(path|url)$`)
	if err != nil {
		return nil, Pin{}, err
	}
	url, err := SubmoduleURL(urlOut, e.Path)
	if err != nil {
		return nil, Pin{}, err
	}
	tree, err := git(ctx, g.Repo, "ls-tree", "HEAD", "--", e.Path)
	if err != nil {
		return nil, Pin{}, err
	}
	commit, err := Gitlink(tree, e.Path)
	if err != nil {
		return nil, Pin{}, err
	}
	dir := filepath.Join(g.Scratch, strings.ReplaceAll(e.ID(), "/", "_"))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, Pin{}, err
	}
	for _, args := range [][]string{
		{"init", "--quiet"},
		{"remote", "add", "origin", url},
		{"sparse-checkout", "init", "--cone"},
		{"sparse-checkout", "set", ".vibesys"},
		{"fetch", "--quiet", "--depth", "1", "--filter=blob:none", "origin", commit},
		{"checkout", "--quiet", "--detach", commit},
	} {
		if _, err := git(ctx, dir, args...); err != nil {
			return nil, Pin{}, err
		}
	}
	return os.DirFS(dir), Pin{URL: url, Revision: commit}, nil
}

func git(ctx context.Context, dir string, args ...string) (string, error) {
	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", dir}, args...)...)
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("git %s: %w: %s", args[0], err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}
