package main

import (
	"context"
	"fmt"
	"strings"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
)

// submitCommand is `submit <kind> <commit> ...`. Only command jobs are queued by this build: the
// check and measure jobs need an environment that can build and benchmark a commit.
func submitCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("submit: name the kind of job: command"))
	}
	switch args[0] {
	case "command":
		return submitJob(ctx, d, args[1:])
	case "check", "measure":
		return fail(d, d.jsonErrors, &unavailable{fmt.Sprintf("submit %s is not available in this build: %s jobs need the host or docker environment", args[0], args[0])})
	default:
		return fail(d, d.jsonErrors, usagef("submit: %q is not a kind of job (command)", args[0]))
	}
}

// unavailable is a refusal the command makes before it calls the daemon.
type unavailable struct{ message string }

func (u *unavailable) Error() string { return u.message }

func submitJob(ctx context.Context, d deps, args []string) int {
	flagArgs, argv, hasCommand := splitDashes(args)
	fs, c := newFlags("submit command", d)
	var (
		env     = fs.String("env", "", "the environment to run in: host or docker (default: the task's)")
		timeout = fs.Duration("timeout", 0, "the time limit of the command, such as 10m (default: the daemon's)")
		service = fs.String("service", "", "a long-lived process to run the command against, split on spaces")
		probe   = fs.String("probe", "", "a command that succeeds when the service is ready, split on spaces")
		note    = fs.String("note", "", "a note to attach to the job")
		noCache = fs.Bool("no-cache", false, "run a new job even when a matching one exists")
		plan    = fs.Uint("plan", 0, "the number of a plan to link the job to")
		wait    = fs.Bool("wait", false, "block until the job is final, then exit as `wait` does")
	)
	positional, err := parse(fs, flagArgs)
	if err != nil {
		return parseFailed(d, fs, err)
	}
	asJSON := c.json
	switch {
	case len(positional) != 1:
		return fail(d, asJSON, usagef("submit command: give one commit, then -- and the command to run"))
	case !hasCommand || len(argv) == 0:
		return fail(d, asJSON, usagef("submit command: give the command to run after --"))
	case *probe != "" && *service == "":
		return fail(d, asJSON, usagef("--probe: needs --service"))
	case *timeout < 0:
		return fail(d, asJSON, usagef("--timeout: %v is negative", *timeout))
	}
	kind, err := environmentKind(*env)
	if err != nil {
		return fail(d, asJSON, err)
	}
	callCtx, err := c.callContext(ctx, d)
	if err != nil {
		return fail(d, asJSON, err)
	}
	commit, err := d.git.Commit(ctx, callCtx.GetRepository(), positional[0])
	if err != nil {
		return fail(d, asJSON, err)
	}

	settings := &cliv1.CommandSettings{Argv: argv}
	if *timeout > 0 {
		settings.Timeout = durationpb.New(*timeout)
	}
	if *service != "" {
		settings.Service = &cliv1.ServiceSpec{Argv: strings.Fields(*service), Probe: strings.Fields(*probe)}
	}
	req := &cliv1.SubmitRequest{
		Context: callCtx,
		Spec: &cliv1.JobSpec{
			Commit: string(commit), Environment: kind,
			Settings: &cliv1.JobSpec_Command{Command: settings},
		},
		NoCache: *noCache, Note: *note, Plan: uint32(*plan),
	}

	cl, err := connect(ctx, d)
	if err != nil {
		return fail(d, asJSON, err)
	}
	defer cl.Close()
	resp, err := cl.Submit(ctx, req)
	if err != nil {
		return fail(d, asJSON, err)
	}
	if asJSON {
		printJSON(d, resp)
	} else {
		fmt.Fprintln(d.stdout, describe(resp.GetJob()))
	}
	if !*wait {
		return exitOK
	}
	return waitFor(ctx, d, cl, callCtx, []string{resp.GetJob().GetId()}, client.WaitOptions{}, asJSON)
}

func environmentKind(name string) (cliv1.EnvironmentKind, error) {
	switch name {
	case "":
		return cliv1.EnvironmentKind_ENVIRONMENT_KIND_UNSPECIFIED, nil
	case "host":
		return cliv1.EnvironmentKind_ENVIRONMENT_KIND_HOST, nil
	case "docker":
		return cliv1.EnvironmentKind_ENVIRONMENT_KIND_DOCKER, nil
	default:
		return 0, usagef("--env: %q is not an environment (host or docker)", name)
	}
}

// connect reaches the daemon, starting one when none runs.
func connect(ctx context.Context, d deps) (*client.Client, error) {
	opts, err := d.options()
	if err != nil {
		return nil, err
	}
	return client.Connect(ctx, opts)
}

// timeoutNote is what a wait that timed out tells its caller, who might otherwise think the jobs
// were stopped.
func timeoutNote(after time.Duration) string {
	return fmt.Sprintf("timed out after %v; the jobs are still going. A timeout never cancels a job.", after)
}
