package production

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// ensureArgv pins the only command used to start the shared CLI daemon and learn where it is.
const ensureArgv = "daemon ensure --json"

// Runner runs an argv with an explicit environment and returns what it wrote. The error is the
// program's status.
type Runner interface {
	Run(ctx context.Context, program string, args, env []string) (stdout, stderr []byte, err error)
}

// ExecRunner is the real program.
type ExecRunner struct{}

// Run implements Runner.
func (ExecRunner) Run(ctx context.Context, program string, args, env []string) ([]byte, []byte, error) {
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.Env = env
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	return stdout.Bytes(), stderr.Bytes(), err
}

// ExecEnsurer asks the recorded tool, never a PATH lookup, in the host's own environment. An empty
// runtime directory lets the tool choose, which is how a run's endpoint is first learned.
type ExecEnsurer struct {
	Endpoint core.JobToolEndpoint
	Runner   Runner
	// Environ is the host's environment, as os.Environ returns it.
	Environ func() []string
}

// ExecEnsurerFor is the ensurer of a real process.
func ExecEnsurerFor(endpoint core.JobToolEndpoint, runner Runner) ExecEnsurer {
	return ExecEnsurer{Endpoint: endpoint, Runner: runner, Environ: os.Environ}
}

// Ensure implements clidaemon.Ensurer.
func (e ExecEnsurer) Ensure(ctx context.Context) (clidaemon.Socket, error) {
	answer, err := e.Answer(ctx)
	if err != nil {
		return clidaemon.Socket{}, err
	}
	return clidaemon.SocketFrom(answer.GetSocket())
}

// Answer runs the tool and reads its typed answer. A tool that says why it has no daemon is quoted: a
// daemon of another build with jobs, or one that cannot start. A tool that answers something else is
// not the tool this host can use.
func (e ExecEnsurer) Answer(ctx context.Context) (*cliv1.DaemonEnsureResponse, error) {
	if _, ok := ctx.Deadline(); !ok {
		return nil, errors.New("ensure requires a deadline")
	}
	binary := e.Endpoint.Executable
	if !filepath.IsAbs(binary) {
		return nil, &clidaemon.Unreadable{Reason: core.CLINotInstalled, Detail: "recorded CLI executable must be absolute"}
	}
	env, err := ensureEnvironment(e.Environ(), e.Endpoint)
	if err != nil {
		return nil, err
	}
	stdout, stderr, err := e.Runner.Run(ctx, binary, strings.Fields(ensureArgv), env)
	switch {
	case errors.Is(err, os.ErrNotExist) || errors.Is(err, os.ErrPermission):
		return nil, &clidaemon.Unreadable{Reason: core.CLINotInstalled, Detail: err.Error()}
	case err != nil:
		return nil, fmt.Errorf("%w: %s", err, failureOf(stderr))
	}
	answer := &cliv1.DaemonEnsureResponse{}
	if err := validAnswer(stdout, answer); err != nil {
		return nil, &clidaemon.Unreadable{Reason: core.CLINotInstalled, Detail: fmt.Sprintf(
			"the job tool %s did not answer where its daemon is (%v); it is not a vibesys-cli this host can use: install this release's, or choose one with `vibesys-client host configure --job-tool PATH`", binary, err)}
	}
	return answer, nil
}

// validAnswer reads the answer. Unknown fields are dropped: a newer tool may add some, and an older
// tool's status, which is what it prints, then fails the rules for having no socket.
func validAnswer(stdout []byte, answer *cliv1.DaemonEnsureResponse) error {
	if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(stdout, answer); err != nil {
		return err
	}
	return protovalidate.Validate(answer)
}

// failureOf is what a tool that exited with no answer said. With --json it is a Failure on stderr,
// whose message is for whoever reads the failure; a program in front of the tool may say anything.
func failureOf(stderr []byte) string {
	failure := &cliv1.Failure{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(stderr, failure); err == nil && failure.GetMessage() != "" {
		return failure.GetMessage()
	}
	return strings.TrimSpace(string(stderr))
}

// ensureEnvironment is the host's environment with the endpoint's place laid over it, and nothing
// else changed: the tool resolves everything else as it would for the host. With no runtime directory
// recorded the variable is unset, so the tool chooses.
func ensureEnvironment(host []string, endpoint core.JobToolEndpoint) ([]string, error) {
	encoded, err := envcodec.Encode(&cliv1.JobToolPlace{RuntimeDirectory: endpoint.RuntimeDirectory})
	if err != nil {
		return nil, fmt.Errorf("the job tool's environment: %w", err)
	}
	env := slices.DeleteFunc(slices.Clone(host), func(entry string) bool {
		name, _, _ := strings.Cut(entry, "=")
		_, set := encoded.Set[name]
		return set || slices.Contains(encoded.Unset, name)
	})
	for name, value := range encoded.Set {
		env = append(env, name+"="+value)
	}
	return env, nil
}
