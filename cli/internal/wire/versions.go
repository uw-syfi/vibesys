package wire

import (
	"fmt"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// BuildVersions returns the contract's message for a build's versions and release version.
func BuildVersions(binaryVersion string, v core.Versions) *cliv1.BuildVersions {
	return &cliv1.BuildVersions{
		BinaryVersion:      binaryVersion,
		ProtocolVersion:    v.Protocol,
		MinProtocolVersion: v.MinProtocol,
		SchemaVersion:      v.Schema,
	}
}

// ToVersions returns the versions a peer sent. The build identity is not a version to order and is
// dropped; DecideDaemon compares it.
func ToVersions(b *cliv1.BuildVersions) core.Versions {
	return core.Versions{Protocol: b.GetProtocolVersion(), MinProtocol: b.GetMinProtocolVersion(), Schema: b.GetSchemaVersion()}
}

// ClientVersions returns the contract's message for the versions this build's command line sends
// in its handshake (core.ClientVersions).
func ClientVersions(binaryVersion string) *cliv1.BuildVersions {
	return BuildVersions(binaryVersion, core.ClientVersions())
}

// CompatibilityError returns nil for a handshake answer of COMPATIBLE, and otherwise the failure
// that tells the caller which side is to upgrade or restart. A value this build does not know is
// read as the client being too old.
func CompatibilityError(answer cliv1.Compatibility) error {
	switch ToCompatibility(answer) {
	case core.Compatible:
		return nil
	case core.DaemonTooOld:
		return &core.Failure{Kind: core.FailureIncompatible, Message: "the running daemon is older than this vibesys-cli; restart it with `vibesys-cli daemon restart`"}
	default:
		return &core.Failure{Kind: core.FailureIncompatible, Message: "this vibesys-cli is older than the running daemon; upgrade it with `vibesys-cli upgrade`"}
	}
}

// DaemonTooOld says a handshake answer is that the daemon is older than the caller.
func DaemonTooOld(answer cliv1.Compatibility) bool {
	return ToCompatibility(answer) == core.DaemonTooOld
}

var compatibilities = []struct {
	domain core.Compatibility
	wire   cliv1.Compatibility
}{
	{core.Compatible, cliv1.Compatibility_COMPATIBILITY_COMPATIBLE},
	{core.ClientTooOld, cliv1.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD},
	{core.DaemonTooOld, cliv1.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD},
}

// Compatibility returns the contract's value for a handshake answer.
func Compatibility(c core.Compatibility) cliv1.Compatibility {
	for _, p := range compatibilities {
		if p.domain == c {
			return p.wire
		}
	}
	return cliv1.Compatibility_COMPATIBILITY_UNSPECIFIED
}

// ToCompatibility returns the domain answer. A value this build does not know is not compatible:
// it is read as the client being too old, since the answer came from a newer peer.
func ToCompatibility(c cliv1.Compatibility) core.Compatibility {
	for _, p := range compatibilities {
		if p.wire == c {
			return p.domain
		}
	}
	return core.ClientTooOld
}

// DaemonDecision is what a command line does with a daemon whose protocol it can speak.
type DaemonDecision = core.DaemonDecision

// The decisions, as core defines them.
const (
	UseDaemon     = core.UseDaemon
	ReplaceDaemon = core.ReplaceDaemon
	RefuseDaemon  = core.RefuseDaemon
)

// SameBuild says the daemon that answered a handshake is the build of the command line. The
// identity is compared as the message carries it.
func SameBuild(clientBuild string, daemon *cliv1.BuildVersions) bool {
	return core.DecideDaemon(clientBuild, daemon.GetBinaryVersion(), 0) == core.UseDaemon
}

// DecideDaemon applies core.DecideDaemon to the daemon's handshake versions and its status.
func DecideDaemon(clientBuild string, daemon *cliv1.BuildVersions, st *cliv1.StatusResponse) DaemonDecision {
	return core.DecideDaemon(clientBuild, daemon.GetBinaryVersion(), LiveWork(st))
}

// LiveWork is everything a daemon's status says it is doing that a restart would touch: the
// submissions it has not answered, whose keying a restart starts over, and its queued and running
// jobs. It is the one count of a daemon's work; a reader that sums some of them instead misses a
// submission that builds its image for hours before its job exists.
func LiveWork(st *cliv1.StatusResponse) uint32 {
	return st.GetSubmittingJobs() + st.GetQueuedJobs() + st.GetRunningJobs()
}

// OtherBuildFailure is the refusal of a daemon of another build that has jobs. It names both
// builds, the daemon, and what to do, so that a reader who knows nothing of daemons can act on it.
func OtherBuildFailure(clientBuild string, st *cliv1.StatusResponse) error {
	name := func(b string) string {
		if b == "" {
			return "unknown"
		}
		return b
	}
	return &core.Failure{Kind: core.FailureDaemonOtherBuild, Message: fmt.Sprintf(
		"a vibesys-cli daemon of another build already runs for you: this vibesys-cli is build %s, the daemon "+
			"(pid %d) is build %s. The daemon has %d submission(s) in progress and %d queued and %d running job(s), so it was not replaced and was "+
			"not used. Wait for the jobs to finish and run the command again, or run `vibesys-cli daemon restart` "+
			"to replace it now; its running jobs keep running under their own supervisors",
		name(clientBuild), st.GetPid(), name(st.GetVersions().GetBinaryVersion()), st.GetSubmittingJobs(), st.GetQueuedJobs(), st.GetRunningJobs())}
}
