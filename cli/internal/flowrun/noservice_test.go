package flowrun_test

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

var updateGolden = flag.Bool("update", false, "rewrite the golden files of the flowrun tests")

// noServiceGolden holds every command a task without [service] issues, for each scenario, as the
// shell issued them before a job could keep a server: it was written at the commit before that
// change and must never be rewritten to follow a change of this package, only of the scenarios.
const noServiceGolden = "testdata/commands_without_a_job_server.golden"

// A task that does not keep a server for the job runs exactly as before the job could keep one:
// every command it issues, a sample's included, and for a task of two images each command's own
// cold start of the candidate's server, is the same, field for field and byte for byte, as the
// golden recorded before the change. Server reuse is opted into, never a default.
func TestATaskWithoutAJobServerIssuesTheSameCommandsAsBefore(t *testing.T) {
	var got strings.Builder
	for _, s := range scenarios() {
		for _, images := range []string{"one image", "two images"} {
			if images == "two images" && s.build != nil {
				continue // a task of two images builds its candidate from a Dockerfile, not a command
			}
			r := newTwoImages(t)
			p := s.plan(false)
			if images == "two images" {
				p = candidatePlan(s, "print", "serving on ${PORT}", ";", "ready", ";", "hold", "forever")
			}
			v, out := r.run(context.Background(), t, p)
			if images == "one image" {
				s.verify(t, v)
			}
			r.noLeaks(t)
			fmt.Fprintf(&got, "== %s, %s\n", s.name, images)
			for _, x := range r.env.Runs() {
				line, err := json.Marshal(x)
				if err != nil {
					t.Fatal(err)
				}
				fmt.Fprintf(&got, "%s\n", line)
			}
			if strings.Contains(out, "[service]") {
				t.Errorf("%s, %s: the output speaks of a job's server:\n%s", s.name, images, out)
			}
		}
	}
	if *updateGolden {
		if err := os.MkdirAll(filepath.Dir(noServiceGolden), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(noServiceGolden, []byte(got.String()), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(noServiceGolden)
	if err != nil {
		t.Fatal(err)
	}
	if got.String() != string(want) {
		t.Fatalf("the commands of a task without a job's server changed:\n got:\n%s\nwant:\n%s", got.String(), want)
	}
}
