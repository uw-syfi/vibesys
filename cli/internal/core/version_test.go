package core_test

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
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

// The command line of this build needs a daemon of protocol 2 (Wait, and the daemon resolving the
// directory), so a daemon of 1 is replaced. The daemon of this build still serves a command line
// and a supervisor of 1.
func TestADaemonOfProtocolOneIsReplacedAndStillServesOldPeers(t *testing.T) {
	old := core.Versions{Protocol: 1, MinProtocol: 1, Schema: core.SchemaVersion}
	if got := core.CheckCompatibility(core.ClientVersions(), old); got != core.DaemonTooOld {
		t.Fatalf("this command line and a daemon of 1: %s, want daemon_too_old", got)
	}
	if got := core.CheckCompatibility(old, core.CurrentVersions()); got != core.Compatible {
		t.Fatalf("a command line of 1 and this daemon: %s, want compatible", got)
	}
	if got := core.CheckCompatibility(core.ClientVersions(), core.CurrentVersions()); got != core.Compatible {
		t.Fatalf("this command line and this daemon: %s", got)
	}
}

// The version WatchTask needs has one definition, the conformance file the other side of the
// contract reads too. This build speaks it, and its command line replaces a daemon below it.
func TestTheProtocolConstantsAreTheConformanceFile(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "proto", "vibesys", "cli", "v1", "conformance", "protocol.json"))
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		WatchTaskSince *uint32 `json:"watch_task_since"`
	}
	dec := json.NewDecoder(bytesReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&file); err != nil || file.WatchTaskSince == nil {
		t.Fatalf("protocol.json: %v, want watch_task_since", err)
	}
	if core.WatchTaskSince != *file.WatchTaskSince {
		t.Errorf("WatchTaskSince = %d, protocol.json says %d", core.WatchTaskSince, *file.WatchTaskSince)
	}
	if core.ProtocolVersion < core.WatchTaskSince {
		t.Errorf("this build speaks protocol %d, below the %d that added WatchTask", core.ProtocolVersion, core.WatchTaskSince)
	}
	// The command line of this build, `daemon ensure` included, replaces a daemon that cannot serve WatchTask.
	older := core.Versions{Protocol: core.WatchTaskSince - 1, MinProtocol: 1, Schema: core.SchemaVersion}
	if got := core.CheckCompatibility(core.ClientVersions(), older); got != core.DaemonTooOld {
		t.Errorf("this command line and a daemon of %d: %s, want daemon_too_old", older.Protocol, got)
	}
	// An orchestrator that needs WatchTask is refused by a daemon below it, and served by this one.
	needs := core.Versions{Protocol: core.WatchTaskSince, MinProtocol: core.WatchTaskSince, Schema: core.SchemaVersion}
	if got := core.CheckCompatibility(needs, older); got != core.DaemonTooOld {
		t.Errorf("a client that needs WatchTask and a daemon of %d: %s, want daemon_too_old", older.Protocol, got)
	}
	if got := core.CheckCompatibility(needs, core.CurrentVersions()); got != core.Compatible {
		t.Errorf("a client that needs WatchTask and this daemon: %s", got)
	}
}

func bytesReader(b []byte) *bytes.Reader { return bytes.NewReader(b) }
