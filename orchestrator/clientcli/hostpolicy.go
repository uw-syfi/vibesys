package clientcli

import (
	"context"
	"errors"
	"fmt"

	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
)

// Finding is what a client finds at a root's socket. The set is closed: Decide has an outcome for
// every value, and a new way for a host to be wrong is a new value here, not a new branch elsewhere.
type Finding int

const (
	// FoundNothing is no host serving the socket.
	FoundNothing Finding = iota
	// FoundCompatible is a host of this client's release, on the configuration in the root.
	FoundCompatible
	// FoundIncompatible is a host of another release.
	FoundIncompatible
	// FoundSilent is a host that was reached and does not answer HostControlService.
	FoundSilent
	// FoundConfigMismatch is a host of this client's release that started on another configuration.
	FoundConfigMismatch
)

// Findings is every Finding, for what must cover them all.
var Findings = []Finding{FoundNothing, FoundCompatible, FoundIncompatible, FoundSilent, FoundConfigMismatch}

// Need is what a verb needs of the host.
type Need int

const (
	// NeedService is to read and steer what exists, which a host of another release can do as long
	// as its protocol is compatible.
	NeedService Need = iota
	// NeedLaunch is to start a run, which must run on this release's host and configuration.
	NeedLaunch
)

// Action is what a client does about a Finding.
type Action int

const (
	// ActionUse uses the host that is there.
	ActionUse Action = iota
	// ActionStart starts a host, for there is none.
	ActionStart
	// ActionReplace asks the host to stop only if it has no live run, and starts this client's.
	ActionReplace
	// ActionRefuse ends the verb with the host left as it is.
	ActionRefuse
	// ActionUnavailable ends the verb because no host can be reached.
	ActionUnavailable
)

// Decision is the one outcome for a Finding, and the exit code when the verb ends there. A verb that
// goes on (use, start, replace) exits with its own code; the decision's says only that nothing here
// ended it.
type Decision struct {
	Action Action
	Exit   int
}

// Decide is the client's policy. It never asks a host with live runs to stop.
func Decide(found Finding, liveRuns uint32, need Need) Decision {
	switch found {
	case FoundNothing:
		return Decision{ActionStart, ExitOK}
	case FoundCompatible:
		return Decision{ActionUse, ExitOK}
	case FoundIncompatible, FoundConfigMismatch:
		switch {
		case liveRuns == 0:
			return Decision{ActionReplace, ExitOK}
		case need == NeedLaunch:
			return Decision{ActionRefuse, ExitFailed}
		}
		return Decision{ActionUse, ExitOK}
	}
	return Decision{ActionUnavailable, ExitUnavailable}
}

// Classify reads what a host said of itself against this client's release and the digest of the
// configuration now in the root.
func Classify(id *pb.IdentifyResponse, clientRelease, configDigest string) Finding {
	switch {
	case id.GetRelease() != clientRelease && id.GetRelation() != pb.ReleaseRelation_RELEASE_RELATION_SAME:
		return FoundIncompatible
	case id.GetConfigDigest() != configDigest:
		return FoundConfigMismatch
	}
	return FoundCompatible
}

// StartRefused is a host that was started and will not serve. Detail is all of why, from this
// attempt alone, and nothing is left running.
type StartRefused struct{ Detail string }

func (e *StartRefused) Error() string { return "the host refused to start: " + e.Detail }

// StartTimedOut is a host that was started and did not come up in time. It was ended.
type StartTimedOut struct{ Err error }

func (e *StartTimedOut) Error() string {
	return "the host did not come up in time: " + e.Err.Error()
}

func (e *StartTimedOut) Unwrap() error { return e.Err }

// placeFailure is the failure of a root that could not be worked out.
func placeFailure(err error) error {
	var place *gatewayroot.Error
	if errors.As(err, &place) {
		return &failure{code: ExitUsage, msg: err.Error()}
	}
	return unavailable("%v", err)
}

// startFailure is the failure of a dial that may have started a host.
func startFailure(err error) error {
	var refused *StartRefused
	var late *StartTimedOut
	switch {
	case errors.As(err, &refused):
		return failed("%v", err)
	case errors.As(err, &late):
		return timedOut("%v", err)
	}
	return unavailable("start the host: %v", err)
}

// startHost makes sure a host serves, and that it is one that came up, before anything else is
// asked of it: a typed refusal would not survive the layers a later call dials through.
func (c *call) startHost(ctx context.Context) error {
	dial, err := c.Connect(c.root)
	if err != nil {
		return placeFailure(err)
	}
	conn, err := dial(ctx)
	if err != nil {
		return startFailure(err)
	}
	return conn.Close()
}

// settle looks at the host, does what Decide says, and returns how to reach the host for the verb.
func (c *call) settle(ctx context.Context, need Need) (Dial, error) {
	id, err := c.identify(ctx)
	var silent error
	found := FoundNothing
	switch {
	case errors.Is(err, ErrNoHost):
	case exitCodeOf(err) == ExitUsage && err != nil:
		return nil, err
	case err != nil:
		found, silent = FoundSilent, err
	default:
		digest, err := c.configDigest()
		if err != nil {
			return nil, err
		}
		found = Classify(id, c.Version, digest)
	}
	switch decision := Decide(found, id.GetLiveRuns(), need); decision.Action {
	case ActionRefuse:
		return nil, failed("not starting a run on this host: %s, with %d %s live, and a host is never replaced under live runs. Let them end or stop them with `%s run stop`, and start again", describeMismatch(found, id, c.Version), id.GetLiveRuns(), plural(id.GetLiveRuns(), "run", "runs"), Program)
	case ActionUnavailable:
		return nil, unavailable("%v. To end it, run `%s host stop --force`", silent, Program)
	case ActionReplace:
		stopped, err := c.stopHost(ctx, true)
		if err != nil {
			// A run started since the host answered, so the answer has changed: decide again.
			return c.settleBusy(found, id, need, err)
		}
		if err := c.startHost(ctx); err != nil {
			return nil, err
		}
		if stopped {
			fmt.Fprintf(c.Stderr, "replaced the host: %s\n", describeMismatch(found, id, c.Version))
		}
	case ActionStart:
		if err := c.startHost(ctx); err != nil {
			return nil, err
		}
	case ActionUse:
		if found != FoundCompatible {
			fmt.Fprintf(c.Stderr, "note: %s, with %d %s live, so it stays until they end\n", describeMismatch(found, id, c.Version), id.GetLiveRuns(), plural(id.GetLiveRuns(), "run", "runs"))
		}
	}
	dial, err := c.Connect(c.root)
	if err != nil {
		return nil, placeFailure(err)
	}
	return dial, nil
}

// settleBusy is what is left when a host that had no live run refused to stop, or could not.
func (c *call) settleBusy(found Finding, id *pb.IdentifyResponse, need Need, stopErr error) (Dial, error) {
	if need == NeedLaunch || exitCodeOf(stopErr) != ExitFailed {
		return nil, stopErr
	}
	fmt.Fprintf(c.Stderr, "note: %s, and a run has just started, so it stays until it ends\n", describeMismatch(found, id, c.Version))
	dial, err := c.Connect(c.root)
	if err != nil {
		return nil, placeFailure(err)
	}
	return dial, nil
}

// describeMismatch says why the host is not the one this client would start.
func describeMismatch(found Finding, id *pb.IdentifyResponse, clientRelease string) string {
	if found == FoundConfigMismatch {
		return "the host started on another configuration than the one now in its root"
	}
	return fmt.Sprintf("the host is release %s and this client is release %s", id.GetRelease(), clientRelease)
}

// configDigest is the digest of the configuration a host started now would read.
func (c *call) configDigest() (string, error) {
	config, err := c.Configs.Load(c.root)
	if err != nil {
		return "", failed("%v", err)
	}
	return hostconfig.Digest(config), nil
}
