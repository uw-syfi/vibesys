package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const validWorkload = `
version = 1
name = "test"
application = "declarative"

[load]
rate = 10
duration_seconds = 1

[[targets]]
name = "api"
protocol = "http"
address = "http://localhost:8080"

[[operations]]
name = "read"
target = "api"
weight = 1

[operations.http]
method = "GET"
path = "/"

[objective]
name = "p50_ms"
metric = "latency_ms.p50"
direction = "minimize"
unit = "ms"
`

func writeWorkload(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "workload.toml")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadAppliesDefaults(t *testing.T) {
	workload, err := Load(writeWorkload(t, validWorkload), "")
	if err != nil {
		t.Fatal(err)
	}
	if workload.Load.Concurrency != 32 || workload.Load.TimeoutSeconds != 10 {
		t.Fatalf("defaults not applied: %+v", workload.Load)
	}
	if workload.Targets[0].SessionPolicy != "reuse" {
		t.Fatalf("target default not applied: %+v", workload.Targets[0])
	}
}

func TestLoadRejectsUnknownField(t *testing.T) {
	_, err := Load(writeWorkload(t, validWorkload+"\nunknown = true\n"), "")
	if err == nil || !strings.Contains(err.Error(), "unknown fields") {
		t.Fatalf("expected unknown-field error, got %v", err)
	}
}

func TestLoadAppliesNamedProfile(t *testing.T) {
	contents := validWorkload + `
[profiles.quick]
rate = 3
duration_seconds = 0.2
[profiles.quick.application_config]
users = 20
`
	workload, err := Load(writeWorkload(t, contents), "quick")
	if err != nil {
		t.Fatal(err)
	}
	if workload.Load.Rate != 3 || workload.Load.DurationSeconds != 0.2 {
		t.Fatalf("profile not applied: %+v", workload.Load)
	}
	if users := workload.ApplicationConfig["users"]; users != int64(20) {
		t.Fatalf("profile application_config users = %#v, want 20", users)
	}
}
