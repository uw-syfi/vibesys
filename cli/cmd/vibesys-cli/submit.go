package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
)

// submitCommand is `submit <kind> <commit> ...`.
func submitCommand(ctx context.Context, d deps, args []string) int {
	if len(args) == 0 {
		return fail(d, d.jsonErrors, usagef("submit: name the kind of job: check, measure, command or profile"))
	}
	switch args[0] {
	case "command":
		return submitJob(ctx, d, args[1:])
	case "profile":
		return submitProfile(ctx, d, args[1:])
	case "check", "measure":
		return submitFlow(ctx, d, args[0], args[1:])
	default:
		return fail(d, d.jsonErrors, usagef("submit: %q is not a kind of job (check, measure, command or profile)", args[0]))
	}
}

// submitProfile is `submit profile <commit> [--wrap prefix]`: the task's [profile] load, run in
// the task's image against the candidate's server, which starts under the wrapper. It is a command
// job, so it is never cached or scored, and its artifacts are kept with it.
func submitProfile(ctx context.Context, d deps, args []string) int {
	flagArgs, extra, hasCommand := splitDashes(args)
	fs, c := newFlags("submit profile", d)
	var (
		wrap    = fs.String("wrap", "", "an argv prefix the candidate's server starts under, such as a profiler's command line, split into words as --service is")
		timeout = fs.Duration("timeout", 0, "the time limit of the load, such as 10m (default: the task's [profile] timeout); the server's startup time is added")
		note    = fs.String("note", "", "a note to attach to the job")
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
		return fail(d, asJSON, usagef("submit profile: give one commit"))
	case hasCommand || len(extra) > 0:
		return fail(d, asJSON, usagef("submit profile: runs the task's [profile] load, so it takes no command after --; use submit command for one of your own"))
	case *timeout < 0:
		return fail(d, asJSON, usagef("--timeout: %v is negative", *timeout))
	}
	prefix, err := splitWords("--wrap", *wrap)
	if err != nil {
		return fail(d, asJSON, err)
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, asJSON, err)
	}
	commit, err := d.commit(ctx, callCtx, positional[0])
	if err != nil {
		return fail(d, asJSON, err)
	}
	settings := &cliv1.CommandSettings{Profile: &cliv1.ProfileRequest{Wrap: prefix}}
	if *timeout > 0 {
		settings.Timeout = durationpb.New(*timeout)
	}
	return send(ctx, d, asJSON, *wait, &cliv1.SubmitRequest{
		Context: callCtx,
		Spec: &cliv1.JobSpec{
			Commit: commit, Environment: cliv1.EnvironmentKind_ENVIRONMENT_KIND_DOCKER, Placement: c.placement,
			Settings: &cliv1.JobSpec_Command{Command: settings},
		},
		Note: *note, Plan: uint32(*plan),
	})
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
	if err == nil {
		err = refuseHostPlacement(fs, *env)
	}
	if err != nil {
		return fail(d, asJSON, err)
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, asJSON, err)
	}
	commit, err := d.commit(ctx, callCtx, positional[0])
	if err != nil {
		return fail(d, asJSON, err)
	}
	spec := &cliv1.JobSpec{Commit: commit, Environment: envKind, Placement: c.placement}
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
		service = fs.String("service", "", "a long-lived process to run the command against, split into words as a shell would, quotes kept")
		probe   = fs.String("probe", "", "a command that succeeds when the service is ready, split as --service is")
		wrap    = fs.String("wrap", "", "an argv prefix, split as --service is, put in front of --service when it is given and of the command otherwise")
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
	if err == nil {
		err = refuseHostPlacement(fs, *env)
	}
	if err != nil {
		return fail(d, asJSON, err)
	}
	callCtx, err := c.callContext(d)
	if err != nil {
		return fail(d, asJSON, err)
	}
	commit, err := d.commit(ctx, callCtx, positional[0])
	if err != nil {
		return fail(d, asJSON, err)
	}

	prefix, err := splitWords("--wrap", *wrap)
	if err != nil {
		return fail(d, asJSON, err)
	}
	settings := &cliv1.CommandSettings{Argv: argv}
	if *timeout > 0 {
		settings.Timeout = durationpb.New(*timeout)
	}
	if *service != "" {
		serviceArgv, err := splitWords("--service", *service)
		if err != nil {
			return fail(d, asJSON, err)
		}
		probeArgv, err := splitWords("--probe", *probe)
		if err != nil {
			return fail(d, asJSON, err)
		}
		if len(serviceArgv) == 0 {
			return fail(d, asJSON, usagef("--service: %q has no command in it", *service))
		}
		settings.Service = &cliv1.ServiceSpec{Argv: append(prefix, serviceArgv...), Probe: probeArgv}
	} else {
		settings.Argv = append(prefix, argv...)
	}
	return send(ctx, d, asJSON, *wait, &cliv1.SubmitRequest{
		Context: callCtx,
		Spec: &cliv1.JobSpec{
			Commit: commit, Environment: kind, Placement: c.placement,
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
	warnNoPlan(d, req, resp.GetJob())
	if !wait {
		return exitOK
	}
	return waitFor(ctx, d, cl, req.GetContext(), []string{resp.GetJob().GetId()}, client.WaitOptions{}, asJSON)
}

// warnNoPlan says on stderr, as plain text even with --json, that a job of an optimization run was
// submitted with no plan: whoever reads the queue cannot tell what it is for, and it takes no plan's
// priority. It refuses nothing and changes no exit code; a person at a terminal, who names no run,
// is not warned.
func warnNoPlan(d deps, req *cliv1.SubmitRequest, job *cliv1.Job) {
	if req.GetContext().GetRunId() == "" || req.GetPlan() != 0 {
		return
	}
	fmt.Fprintf(d.stderr, "vibesys-cli: warning: job %s of run %s has no --plan; link every job of a run to the plan it serves (--plan <n>), so the queue says what it is for and it takes the plan's priority\n",
		job.GetId(), req.GetContext().GetRunId())
}

// refuseHostPlacement refuses a --placement given on the command line beside --env host: a host job
// runs on this machine and claims no placement, so it could not run where the flag says. A
// placement that only comes from VIBESYS_PLACEMENT is not refused, since it is the default every
// submit inherits for its docker jobs, and the daemon records no placement for a host job.
func refuseHostPlacement(fs *flag.FlagSet, env string) error {
	if env != "host" {
		return nil
	}
	var err error
	fs.Visit(func(f *flag.Flag) {
		if f.Name == "placement" && f.Value.String() != "" {
			err = usagef("--placement: a host job (--env host) runs on this machine and claims no placement; drop --placement or use --env docker")
		}
	})
	return err
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
