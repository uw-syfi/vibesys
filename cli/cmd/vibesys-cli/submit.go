package main

import (
	"context"
	"fmt"
	"math"
	"strings"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
)

// submitCommand is `submit <kind> <commit> ...`.
func submitCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, false, usagef("submit: name the kind of job: check, measure or command"))
	}
	switch args[0] {
	case "command":
		return submitJob(ctx, d, args[1:])
	case "check", "measure":
		return submitFlow(ctx, d, args[0], args[1:])
	default:
		return fail(d, false, usagef("submit: %q is not a kind of job (check, measure or command)", args[0]))
	}
}

// unavailable is a refusal the command makes before it calls the daemon.
type unavailable struct{ message string }

func (u *unavailable) Error() string { return u.message }

// submitFlow is `submit check|measure <commit>`: it runs the task's own commands, so it takes no
// command of its own.
func submitFlow(ctx context.Context, d deps, kind string, args []string) int {
	flagArgs, extra, hasCommand := splitDashes(args)
	fs, c := newFlags("submit "+kind, d)
	var (
		env     = fs.String("env", "", "the environment to run in: host or docker (default: the task's)")
		note    = fs.String("note", "", "a note to attach to the job")
		noCache = fs.Bool("no-cache", false, "run the whole job again, with a fresh check, even when saved results answer it")
		plan    = fs.Uint("plan", 0, "the number of a plan to link the job to")
		wait    = fs.Bool("wait", false, "block until the job is final, then exit as `wait` does")
		samples = fs.Uint("samples", 0, "measure: how many benchmark samples are needed (default: the task's)")
		recheck = fs.Bool("recheck", false, "measure: run the check again even when a saved pass exists")
	)
	positional, err := parse(fs, flagArgs)
	if err != nil {
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
	}
	asJSON := c.json
	switch {
	case len(positional) != 1:
		return fail(d, asJSON, usagef("submit %s: give one commit", kind))
	case hasCommand || len(extra) > 0:
		return fail(d, asJSON, usagef("submit %s: runs the task's own commands, so it takes none after --", kind))
	case kind == "check" && *samples != 0:
		return fail(d, asJSON, usagef("--samples: a check job takes no samples"))
	case kind == "check" && *recheck:
		return fail(d, asJSON, usagef("--recheck: a check job always runs its check"))
	case *samples > math.MaxUint32:
		return fail(d, asJSON, usagef("--samples: %d is too many", *samples))
	}
	envKind, err := environmentKind(*env)
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
	spec := &cliv1.JobSpec{Commit: string(commit), Environment: envKind}
	if kind == "check" {
		spec.Settings = &cliv1.JobSpec_Check{Check: &cliv1.CheckSettings{}}
	} else {
		spec.Settings = &cliv1.JobSpec_Measure{Measure: &cliv1.MeasureSettings{Samples: uint32(*samples), Recheck: *recheck}}
	}
	return send(ctx, d, asJSON, *wait, &cliv1.SubmitRequest{
		Context: callCtx, Spec: spec, NoCache: *noCache, Note: *note, Plan: uint32(*plan),
	})
}

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
		if isHelp(err) {
			return exitOK
		}
		return exitInvalidInput
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
	return send(ctx, d, asJSON, *wait, &cliv1.SubmitRequest{
		Context: callCtx,
		Spec: &cliv1.JobSpec{
			Commit: string(commit), Environment: kind,
			Settings: &cliv1.JobSpec_Command{Command: settings},
		},
		NoCache: *noCache, Note: *note, Plan: uint32(*plan),
	})
}

// send submits, prints the job, and with wait blocks until it is final.
func send(ctx context.Context, d deps, asJSON, wait bool, req *cliv1.SubmitRequest) int {
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
		fmt.Fprintln(d.stdout, describeSubmitted(resp))
	}
	if !wait {
		return exitOK
	}
	return waitFor(ctx, d, cl, req.GetContext(), []string{resp.GetJob().GetId()}, client.WaitOptions{}, asJSON)
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
	return fmt.Sprintf("vibesys-cli: timed out after %v; the jobs are still going. A timeout never cancels a job.\n", after)
}
