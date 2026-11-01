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
	ProtocolVersion uint32 = 1

	// MinProtocolVersion is the oldest ProtocolVersion this build still serves.
	MinProtocolVersion uint32 = 1
)

// Versions is what one build carries, less the release version, which is display only and which
// no caller compares.
type Versions struct {
	Protocol    uint32
	MinProtocol uint32
	Schema      uint32
}

// CurrentVersions returns the versions of this build.
func CurrentVersions() Versions {
	return Versions{Protocol: ProtocolVersion, MinProtocol: MinProtocolVersion, Schema: SchemaVersion}
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
