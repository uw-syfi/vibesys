package filestore_test

import (
	"strings"
	"testing"

	"google.golang.org/protobuf/reflect/protoreflect"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

// A store of version 9, written before a command job could declare output files, opens as a store
// of version 10, and each of its jobs reads as one that declared none and kept none: nothing is
// guessed for a job saved before.
func TestAStoreOfVersionNineReadsItsJobsWithNoOutputs(t *testing.T) {
	mem := fsys.NewMemFS()
	s := openOn(t, mem)
	for _, o := range script() {
		if err := o.run(t.Context(), s); err != nil {
			t.Fatalf("%s: %v", o.name, err)
		}
	}
	image := mem.CrashImage(false)
	for name, data := range image.Files() {
		if strings.HasPrefix(string(data), "{") && !strings.HasSuffix(name, ".lock") {
			image.Put(name, withVersion(data, "9"))
		}
	}
	opened := openOn(t, image)
	if got := formatOf(t, image); got != 10 {
		t.Fatalf("format after the migration = %d, want 10", got)
	}
	jobs, err := opened.Jobs(t.Context(), core.JobFilter{})
	if err != nil || len(jobs) == 0 {
		t.Fatalf("Jobs = %v, %v", jobs, err)
	}
	for _, j := range jobs {
		if c, ok := j.Spec.Settings.(core.CommandSettings); (ok && c.Outputs) || j.Outputs != nil {
			t.Errorf("job %s of a store of version 9 reads with outputs %+v", j.ID, j.Outputs)
		}
	}
}

// fieldsAddedBy names the fields of a job record and of a command's settings under the format version
// that added them, as the comment on formatVersion records it: version 2 for the fields of the first
// protojson store (submitters were added within it), 5 for a job's needs and task version, 8 for its
// GPUs and 10 for its outputs. Fields added within a version without raising it (a failed stage, and a
// command's profile, candidate, image and wrap) are named under 8, the version every store held them
// by. A field added to either message is named here under the version that adds it, and no version
// named is newer than the one a fresh store is written at, so a program of an older version refuses
// the store and never a job in it.
var fieldsAddedBy = map[string]map[string]int{
	"Job": {
		"id": 2, "spec": 2, "state": 2, "key": 2, "environment": 2, "notes": 2, "plans": 2, "submitted_at": 2,
		"started_at": 2, "finished_at": 2, "cancel_reason": 2, "exit_code": 2, "result_job_id": 2, "check_job_id": 2,
		"no_cache": 2, "attempts": 2, "submitters": 2, "needs": 5, "task_version": 5, "failed_stage": 8, "gpus": 8,
		"outputs": 10,
	},
	"CommandSettings": {
		"argv": 2, "service": 2, "timeout": 2, "profile": 8, "candidate": 8, "image": 8, "wrap": 8, "outputs": 10,
	},
}

func TestEveryFieldOfAJobAndACommandIsWrittenAtAVersionThatKnowsIt(t *testing.T) {
	mem := fsys.NewMemFS()
	if err := openOn(t, mem).Close(); err != nil {
		t.Fatal(err)
	}
	version := formatOf(t, mem)
	check := func(message string, fields protoreflect.FieldDescriptors) {
		for i := range fields.Len() {
			name := string(fields.Get(i).Name())
			added, ok := fieldsAddedBy[message][name]
			switch {
			case !ok:
				t.Errorf("%s.%s is not in fieldsAddedBy: name the format version that adds it, and raise the store's format if it is newer", message, name)
			case added > version:
				t.Errorf("%s.%s was added in format %d, and a fresh store is written at format %d", message, name, added, version)
			}
		}
	}
	check("Job", (&cliv1.Job{}).ProtoReflect().Descriptor().Fields())
	check("CommandSettings", (&cliv1.CommandSettings{}).ProtoReflect().Descriptor().Fields())
}
