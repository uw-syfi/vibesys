package core_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func TestThisBuildSpeaksItself(t *testing.T) {
	v := core.CurrentVersions()
	if v.Protocol != core.ProtocolVersion || v.MinProtocol != core.MinProtocolVersion || v.Schema != core.SchemaVersion {
		t.Errorf("CurrentVersions() = %+v, want the constants", v)
	}
	if v.Protocol == 0 || v.Schema == 0 || v.MinProtocol == 0 || v.MinProtocol > v.Protocol {
		t.Errorf("versions %+v: they start at 1 and the minimum cannot exceed the version", v)
	}
	if got := core.CheckCompatibility(v, v); got != core.Compatible {
		t.Errorf("a build is %s with itself", got)
	}
}

// The handshake, over every pair of builds with protocols 1 to 5 whose minimum is at most their
// protocol: 15 builds, 225 pairs.
func TestCompatibilityOverEveryPairOfBuilds(t *testing.T) {
	var builds []core.Versions
	for protocol := uint32(1); protocol <= 5; protocol++ {
		for minimum := uint32(1); minimum <= protocol; minimum++ {
			builds = append(builds, core.Versions{Protocol: protocol, MinProtocol: minimum, Schema: 1})
		}
	}
	for _, client := range builds {
		for _, daemon := range builds {
			got := core.CheckCompatibility(client, daemon)
			clientServed := client.Protocol >= daemon.MinProtocol
			daemonServed := daemon.Protocol >= client.MinProtocol
			var want core.Compatibility
			switch {
			case !clientServed:
				want = core.ClientTooOld
			case !daemonServed:
				want = core.DaemonTooOld
			default:
				want = core.Compatible
			}
			if got != want {
				t.Errorf("client %+v with daemon %+v: %s, want %s", client, daemon, got, want)
			}
			// Compatibility is mutual: when each serves the other, swapping the roles agrees.
			if got == core.Compatible && core.CheckCompatibility(daemon, client) != core.Compatible {
				t.Errorf("client %+v with daemon %+v is compatible, and the swapped roles are not", client, daemon)
			}
		}
	}
}
