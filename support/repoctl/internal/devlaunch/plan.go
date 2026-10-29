package devlaunch

import (
	"fmt"
	"strings"
)

// Options are the command-line choices that shape a plan.
type Options struct {
	// Machine is the ssh alias to ship to, or empty for a local-only run.
	Machine string
	// Checkout is the machine's checkout path when the user names it.
	Checkout string
	// DryRun prints the plan and changes nothing; the plan itself does not depend on it.
	DryRun bool
}

// InstallState is what the shell knows about the installed client packages.
type InstallState struct {
	// Fingerprint is the digest of the lockfile and package manifests now.
	Fingerprint string
	// Stamp is the digest recorded by the last successful install, or "" for none.
	Stamp string
	// ModulesPresent is false when the installed packages are gone, whatever the stamp says.
	ModulesPresent bool
}

// LocalGit is the local tree's state.
type LocalGit struct {
	Head string
	// Dirty is true when a tracked file differs from HEAD.
	Dirty bool
}

// CheckoutKind says what a machine's checkout path holds.
type CheckoutKind string

const (
	// CheckoutGit is the top of a git working tree.
	CheckoutGit CheckoutKind = "git"
	// CheckoutPlain is a directory that is not a git working tree: files are copied in.
	CheckoutPlain CheckoutKind = "plain"
	// CheckoutMissing means no candidate path exists on the machine.
	CheckoutMissing CheckoutKind = "missing"
)

// RemoteState is what the shell found on the machine.
type RemoteState struct {
	Kind CheckoutKind
	// Checkout is the absolute path found (empty when Kind is CheckoutMissing).
	Checkout string
	// Tried lists the candidate paths looked at, for the message when none exists.
	Tried []string
	// Head, Branch and Dirty are known for CheckoutGit only.
	Head   string
	Branch string
	Dirty  bool
}

// Inputs is everything the planner decides from. Remote is nil for a local-only run.
type Inputs struct {
	Install InstallState
	Git     LocalGit
	Remote  *RemoteState
}

// StepKind names one unit of work.
type StepKind string

const (
	StepInstall   StepKind = "install"
	StepBuild     StepKind = "build"
	StepPython    StepKind = "python"
	StepShipGit   StepKind = "ship-git"
	StepShipFiles StepKind = "ship-files"
	StepSync      StepKind = "sync"
	StepHelper    StepKind = "helper"
	StepApp       StepKind = "app"
)

// Step is one planned action; Reason says why it is in the plan.
type Step struct {
	Kind   StepKind
	Label  string
	Reason string
}

// Refusal is a plan that must not run, with what the user should do about it.
type Refusal struct{ Reason string }

func (r Refusal) Error() string { return r.Reason }

// InstallNeeded reports whether the client packages must be installed.
func InstallNeeded(s InstallState) (bool, string) {
	switch {
	case !s.ModulesPresent:
		return true, "packages are not installed"
	case s.Stamp == "":
		return true, "no record of a successful install"
	case s.Stamp != s.Fingerprint:
		return true, "lockfile or manifests changed since the last install"
	}
	return false, "lockfile and manifests unchanged since the last install"
}

// PlanSteps decides the ordered steps, or refuses before any of them runs.
func PlanSteps(in Inputs) ([]Step, error) {
	if in.Remote != nil {
		if err := refuseRemote(in); err != nil {
			return nil, err
		}
	}
	var steps []Step
	if need, why := InstallNeeded(in.Install); need {
		steps = append(steps, Step{StepInstall, "install client packages", why})
	}
	steps = append(steps, Step{StepBuild, "build client packages", "always: the build tool skips what is current"})
	steps = append(steps, Step{StepPython, "install Python dependencies here", "This Mac's helper starts without syncing; a no-op when current"})
	if r := in.Remote; r != nil {
		steps = append(steps, shipSteps(in.Git, *r)...)
		steps = append(steps,
			Step{StepSync, "install Python dependencies on the machine", "the helper starts without syncing"},
			Step{StepHelper, "start the machine's helper and wait for its hello", "a fresh helper reads the checkout as it is now"},
		)
	}
	return append(steps, Step{StepApp, "open the desktop app", "own profile directory"}), nil
}

func refuseRemote(in Inputs) error {
	r := in.Remote
	switch {
	case in.Git.Dirty:
		return Refusal{"the local tree has uncommitted changes to tracked files; commit them, because only the committed HEAD is shipped"}
	case r.Kind == CheckoutMissing:
		return Refusal{fmt.Sprintf("no VibeSys checkout at %s on the machine; fix the path (--checkout PATH) or set the machine up again in the app", strings.Join(r.Tried, ", "))}
	case r.Kind == CheckoutGit && r.Dirty:
		return Refusal{fmt.Sprintf("the checkout on the machine (%s) has uncommitted changes to tracked files; commit or discard them there", r.Checkout)}
	}
	return nil
}

func shipSteps(local LocalGit, r RemoteState) []Step {
	if r.Kind == CheckoutPlain {
		return []Step{{StepShipFiles, "copy tracked files to the machine (rsync)", "the checkout is not a git working tree"}}
	}
	if r.Head == local.Head {
		return nil
	}
	return []Step{{StepShipGit, "push HEAD to the machine and check it out", "the machine is at another commit"}}
}
