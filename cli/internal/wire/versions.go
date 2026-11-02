package wire

import (
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

// ToVersions returns the versions a peer sent. The release version is display only and is
// dropped.
func ToVersions(b *cliv1.BuildVersions) core.Versions {
	return core.Versions{Protocol: b.GetProtocolVersion(), MinProtocol: b.GetMinProtocolVersion(), Schema: b.GetSchemaVersion()}
}

// CurrentVersions returns the contract's message for this build's versions, as a handshake sends
// them.
func CurrentVersions(binaryVersion string) *cliv1.BuildVersions {
	return BuildVersions(binaryVersion, core.CurrentVersions())
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
