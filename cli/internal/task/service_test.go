package task_test

import (
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// serviceSection is a job-scoped server, as a task of one image declares it.
const serviceSection = `
[service]
command = ["python3", "${PROJECT_ROOT}/.vibesys/tasks/t/_server.py", "--port", "${PORT}"]
ready = "/health"
startup_timeout_seconds = 3600

[service.reset]
command = ["python3", "${PROJECT_ROOT}/.vibesys/tasks/t/reset.py"]
timeout_seconds = 300
`

// withService is the 0.2 task of one image with a job-scoped server.
func withService() string { return withoutCandidate() + serviceSection }

func TestATaskOfOneImageReadsItsJobScopedServer(t *testing.T) {
	got, err := task.Read(repo(v02Files(withService())), "t")
	if err != nil {
		t.Fatal(err)
	}
	s := got.Service
	if s == nil {
		t.Fatal("no service")
	}
	if s.Ready != "/health" || s.StartupTimeout != time.Hour || s.ShareWithCheck {
		t.Fatalf("service %+v", s)
	}
	if got := strings.Join(s.ServeAt("/work"), " "); got != "python3 /work/.vibesys/tasks/t/_server.py --port ${PORT}" {
		t.Fatalf("serve %q", got)
	}
	if got := strings.Join(s.Reset.Resolve("/work"), " "); got != "python3 /work/.vibesys/tasks/t/reset.py" || s.Reset.Timeout != 5*time.Minute {
		t.Fatalf("reset %q in %v", got, s.Reset.Timeout)
	}
	if got.Candidate != nil || got.Setup().TwoImages {
		t.Fatalf("a task of one image read as %+v", got.Setup())
	}
	shared, err := task.Parse("t", []byte(withService()+"\n[service]\n"), nil)
	if err == nil {
		t.Fatalf("a second [service] table was read: %+v", shared.Service)
	}
	shared, err = task.Parse("t", []byte(strings.Replace(withService(), `ready = "/health"`, "ready = \"/health\"\nshare_with_accuracy = true", 1)), []byte(v02Objectives))
	if err != nil || !shared.Service.ShareWithCheck {
		t.Fatalf("share_with_accuracy: %+v, %v", shared.Service, err)
	}
}

// A task with no [service] has no job-scoped server: each command starts what it needs.
func TestATaskWithoutAServiceHasNone(t *testing.T) {
	got, err := task.Read(repo(v02Files(withoutCandidate())), "t")
	if err != nil || got.Service != nil {
		t.Fatalf("read %+v, %v", got.Service, err)
	}
}

// What [service] may not say is refused when the task is read, naming the key or the rule.
func TestWhatAServiceMayNotSayIsRefusedNamingIt(t *testing.T) {
	cases := []struct {
		name, from, to, says string
	}{
		{"an unknown key", `ready = "/health"`, "ready = \"/health\"\nport = 8000", `"service.port"`},
		{"an unknown reset key", `timeout_seconds = 300`, "timeout_seconds = 300\nretries = 2", `"service.reset.retries"`},
		{"no command", `command = ["python3", "${PROJECT_ROOT}/.vibesys/tasks/t/_server.py", "--port", "${PORT}"]`, ``, "command"},
		{"a command that does not name its port", `"--port", "${PORT}"]`, `"--port", "30000"]`, "${PORT}"},
		{"an unknown placeholder", `"--port", "${PORT}"]`, `"--port", "${PORT}", "--host", "${HOST}"]`, "${HOST}"},
		{"a ready that is not a path", `ready = "/health"`, `ready = "health"`, "ready"},
		{"no startup timeout", `startup_timeout_seconds = 3600`, ``, "startup_timeout_seconds"},
		{"a zero startup timeout", `startup_timeout_seconds = 3600`, `startup_timeout_seconds = 0`, "startup_timeout_seconds"},
		{"no reset", "[service.reset]\ncommand = [\"python3\", \"${PROJECT_ROOT}/.vibesys/tasks/t/reset.py\"]\ntimeout_seconds = 300\n", ``, "reset"},
		{"a reset with no command", `command = ["python3", "${PROJECT_ROOT}/.vibesys/tasks/t/reset.py"]`, ``, "reset"},
		{"a port in the reset", `"${PROJECT_ROOT}/.vibesys/tasks/t/reset.py"]`, `"${PROJECT_ROOT}/.vibesys/tasks/t/reset.py", "${PORT}"]`, "${PORT}"},
		{"a reset given as an evaluator", `timeout_seconds = 300`, "timeout_seconds = 300\nentrypoint = \"x\"", `"service.reset.entrypoint"`},
		{"share_with_accuracy that is not a boolean", `ready = "/health"`, "ready = \"/health\"\nshare_with_accuracy = \"yes\"", "share_with_accuracy"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if !strings.Contains(withService(), c.from) {
				t.Fatalf("the case does not apply: %q", c.from)
			}
			manifest := strings.Replace(withService(), c.from, c.to, 1)
			_, err := task.Parse("t", []byte(manifest), []byte(v02Objectives))
			if f := failureOf(t, err); !strings.Contains(f.Message, c.says) {
				t.Fatalf("got %+v, want it to name %q", f, c.says)
			}
		})
	}
}

// A task of two images starts its candidate's server for each command, and cannot also keep a
// server for the job.
func TestATaskOfTwoImagesHasNoJobScopedServer(t *testing.T) {
	_, err := task.Parse("t", []byte(v02Manifest+serviceSection), []byte(v02Objectives))
	if f := failureOf(t, err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "[service]") || !strings.Contains(f.Message, "[candidate]") {
		t.Fatalf("got %+v", f)
	}
}

// The server runs in a container of the task's image, so a task with one needs its Dockerfile.
func TestATaskWithAServiceNeedsItsOwnDockerfile(t *testing.T) {
	files := v02Files(withService())
	delete(files, ".vibesys/tasks/t/Dockerfile")
	_, err := task.Read(repo(files), "t")
	if f := failureOf(t, err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "[service]") || !strings.Contains(f.Message, "Dockerfile") {
		t.Fatalf("got %+v", f)
	}
}

// The single-image format 0.1 has no [service]: a manifest of it with one is refused, not read
// without it.
func TestSchema01ReadsNoService(t *testing.T) {
	if _, err := task.Parse("t", []byte(protocol2Manifest+serviceSection), nil); err == nil || !strings.Contains(err.Error(), "service") {
		t.Fatalf("got %v", err)
	}
}
