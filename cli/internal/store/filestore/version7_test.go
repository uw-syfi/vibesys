package filestore_test

import (
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

// jobSpecFieldsAddedBy names, for each field of a job's spec, the format version that added it. A
// field added to JobSpec is named here under the version that adds it, and that version is the one
// the program writes: a program of an older version refuses the field as unknown, so it must refuse
// the whole store first, by its format file, instead of failing on the first job that sets the field.
var jobSpecFieldsAddedBy = map[string]int{
	"commit": 2, "environment": 2, "check": 2, "command": 2, "measure": 2,
	"placement":  4,
	"allocation": 7,
	"trace":      8,
}

// Every field of a job's spec is named under the version that added it, and no version named is
// newer than the one a fresh store is written at. Before the store's format was raised for a job's
// standing allocation, a store of format 6 held jobs a program of format 6 refused to read.
func TestEveryFieldOfAJobSpecIsWrittenAtAVersionThatKnowsIt(t *testing.T) {
	mem := fsys.NewMemFS()
	if err := openOn(t, mem).Close(); err != nil {
		t.Fatal(err)
	}
	version := formatOf(t, mem)
	fields := (&cliv1.JobSpec{}).ProtoReflect().Descriptor().Fields()
	for i := range fields.Len() {
		name := string(fields.Get(i).Name())
		added, ok := jobSpecFieldsAddedBy[name]
		switch {
		case !ok:
			t.Errorf("JobSpec.%s is not in jobSpecFieldsAddedBy: name the format version that adds it, and raise the store's format if it is newer", name)
		case added > version:
			t.Errorf("JobSpec.%s was added in format %d, and a fresh store is written at format %d: a program of format %d would open the store and refuse its jobs", name, added, version, version)
		}
	}
}
