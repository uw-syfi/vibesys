package core

// The versions this build speaks. Each is defined here once; every other use, the cache key, the
// version handshake and the records the store keeps, takes it from here.
const (
	// SchemaVersion is the revision of the stored data and of the cache key. It is part of every
	// cache key, so raising it retires every cached result. Raise it whenever a change makes an
	// older result mean something different: the key's encoding, a rule that decides what a
	// result is, or the stored shape of a result.
	SchemaVersion uint32 = 1

	// ProtocolVersion is the revision of the CLI, daemon and supervisor contract this build
	// speaks. It starts at 1 and rises only when a change breaks an older peer.
	//
	// 2: the command line sends its working directory, which the daemon resolves to a repository,
	// and waits through Wait, whose outcome the daemon decides. A daemon of 1 has neither.
	//
	// 3: WatchTask streams a task's state as facts, and the handshake accepts the orchestrator as
	// a component. A daemon of 2 has neither, and rejects the component.
	//
	// 4: a submission names the placement its docker job runs on. A daemon of 3 would drop the
	// field and run the job on its one placement, which is not what was asked.
	//
	// 5: a task of schema 0.2 (two images, an evaluator package) is read and run. A build of 4 or
	// below reads only schema 0.1 and refuses a manifest with an [evaluator] table, which the
	// orchestrator reads as a job tool too old for its tasks (TaskSchema02Since).
	//
	// 6: standing allocations. A submission may name the allocation its job runs in (JobSpec.allocation),
	// and the allocation calls exist. A daemon of 5 would drop the field and run the job outside the
	// allocation it was asked to run in, so a command line sends it only to a daemon of 6
	// (AllocationsSince). MinDaemonProtocolVersion stays 4: a command line that names no allocation
	// works with an older daemon as it always did.
	ProtocolVersion uint32 = 6

	// MinProtocolVersion is the oldest ProtocolVersion this build still serves. A daemon still
	// serves command lines and supervisors of 1: an older command line waits through Watch and
	// sends a repository root, which resolves as any directory in it does.
	MinProtocolVersion uint32 = 1

	// MinDaemonProtocolVersion is the oldest daemon the command line of this build works with. It
	// is above MinProtocolVersion because the command line needs what 2 added, while a daemon of
	// this build still serves older peers. It is at least WatchTaskSince, so a command line of this
	// build, `daemon ensure` included, replaces a daemon that cannot serve WatchTask, and it is 4,
	// so that it replaces one that would drop the placement of a submission.
	MinDaemonProtocolVersion uint32 = 4
)

// WatchTaskSince is the protocol version that added WatchTask. It is the one definition of the
// version a client needs of a daemon to watch a task; conformance/protocol.json holds the same
// number for the other side of the contract, and a test holds this to it.
const WatchTaskSince uint32 = 3

// TaskSchema02Since is the protocol version that added task schema 0.2. It is the one definition of
// the version a job tool needs to run the tasks this release ships; conformance/protocol.json holds
// the same number for the orchestrator, which asks the tool for its version before it starts a run,
// and a test holds this to it.
const TaskSchema02Since uint32 = 5

// AllocationsSince is the protocol version that added standing allocations. A command line that
// names an allocation needs a daemon of at least this, and refuses an older one rather than have it
// drop the allocation.
const AllocationsSince uint32 = 6

// Versions is what one build carries, less the build identity (BuildVersions.binary_version),
// which names a build and is not a version to order.
type Versions struct {
	Protocol    uint32
	MinProtocol uint32
	Schema      uint32
}

// CurrentVersions returns the versions of this build.
func CurrentVersions() Versions {
	return Versions{Protocol: ProtocolVersion, MinProtocol: MinProtocolVersion, Schema: SchemaVersion}
}

// ClientVersions returns the versions the command line of this build sends in its handshake: its
// minimum is the oldest daemon it works with, so an older daemon answers DaemonTooOld and is
// replaced.
func ClientVersions() Versions {
	return Versions{Protocol: ProtocolVersion, MinProtocol: MinDaemonProtocolVersion, Schema: SchemaVersion}
}

// Compatibility is the answer to a handshake, from the daemon's side.
type Compatibility string

// The answers.
const (
	// Compatible means the two builds can talk.
	Compatible Compatibility = "compatible"
	// ClientTooOld means the client's protocol is below the daemon's minimum: upgrade the client.
	ClientTooOld Compatibility = "client_too_old"
	// DaemonTooOld means the daemon's protocol is below the client's minimum: restart the daemon
	// from the client's binary. Running jobs survive the restart.
	DaemonTooOld Compatibility = "daemon_too_old"
)

// CheckCompatibility says whether a client and a daemon can talk. Each side serves a peer whose
// protocol is at least its own minimum; when neither does, the client is the one to upgrade.
func CheckCompatibility(client, daemon Versions) Compatibility {
	switch {
	case client.Protocol < daemon.MinProtocol:
		return ClientTooOld
	case daemon.Protocol < client.MinProtocol:
		return DaemonTooOld
	default:
		return Compatible
	}
}

// DaemonDecision is what a command line does with a daemon that answered its handshake with
// COMPATIBLE.
type DaemonDecision string

// The decisions.
const (
	// UseDaemon: the daemon is the same build as the command line.
	UseDaemon DaemonDecision = "use"
	// ReplaceDaemon: the daemon is another build and has no job and no submission, so the command
	// line stops it and starts its own build. The user ran one install command and manages no
	// daemon.
	ReplaceDaemon DaemonDecision = "replace"
	// RefuseDaemon: the daemon is another build and has a submission in progress or a job queued or
	// running. It is left as it is, and the command says so, naming both builds.
	RefuseDaemon DaemonDecision = "refuse"
)

// DecideDaemon is the one rule for a daemon of a compatible protocol. Build identities are the
// strings builds report as BuildVersions.binary_version: equal means the same build, and they are
// never parsed or ordered, so a release build and a local build compare the same way. A daemon that
// reports no identity (an empty string) is another build than any command line, which always has
// one. live is the daemon's work: its unanswered submissions and its queued and running jobs.
func DecideDaemon(client, daemon string, live uint32) DaemonDecision {
	switch {
	case client != "" && client == daemon:
		return UseDaemon
	case live == 0:
		return ReplaceDaemon
	default:
		return RefuseDaemon
	}
}
