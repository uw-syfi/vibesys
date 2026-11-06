package task

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"google.golang.org/protobuf/reflect/protoreflect"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/tomlproto"
)

// SchemaVersion is a task schema version, MAJOR.MINOR, as `version` in the manifest declares it.
// Schema versions are their own numbers: they follow neither this binary's release nor the
// evaluator's.
type SchemaVersion = tomlproto.Version

// The schema versions this build reads. While the major version is 0 every minor version is a
// format of its own, which a reader accepts exactly; from 1.0 on a reader of M.N accepts M.0 to M.N.
var (
	// Schema01 is the single-image format: one image, the task's, runs every command.
	Schema01 = SchemaVersion{Major: 0, Minor: 1}
	// Schema02 is the format of two images, TaskManifestV02 in task.proto.
	Schema02 = schemaOf((&cliv1.TaskManifestV02{}).ProtoReflect().Descriptor())
)

func schemaOf(md protoreflect.MessageDescriptor) SchemaVersion {
	v, ok := tomlproto.Schema(md)
	if !ok {
		panic(fmt.Sprintf("task: the manifest message %s declares no task_schema", md.FullName()))
	}
	return v
}

// Schemas lists the schema versions this build reads, oldest first.
func Schemas() []SchemaVersion { return []SchemaVersion{Schema01, Schema02} }

// SchemasText names the schema versions this build reads, for a person: "0.1, 0.2".
func SchemasText() string {
	names := make([]string, 0, len(Schemas()))
	for _, v := range Schemas() {
		names = append(names, v.String())
	}
	return strings.Join(names, ", ")
}

var versionPattern = regexp.MustCompile(`^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$`)

// ParseSchemaVersion reads a "MAJOR.MINOR" version. Each part is a number without a leading zero.
func ParseSchemaVersion(s string) (SchemaVersion, error) {
	m := versionPattern.FindStringSubmatch(s)
	if m == nil {
		return SchemaVersion{}, invalid("%s: version %q is not a schema version: write it as MAJOR.MINOR in quotes, such as version = %q", ManifestName, s, Schema02.String())
	}
	major, _ := strconv.ParseUint(m[1], 10, 32)
	minor, _ := strconv.ParseUint(m[2], 10, 32)
	return SchemaVersion{Major: uint32(major), Minor: uint32(minor)}, nil
}

// declaredVersion reads `version` from a manifest's top-level table and says which schema this
// build reads the file with. It refuses a version that is missing, misspelled, older than any it
// reads, or newer, which a newer vibesys-cli may read.
func declaredVersion(table map[string]any) (SchemaVersion, error) {
	raw, ok := table["version"]
	if !ok {
		return SchemaVersion{}, invalid("%s: no version; declare the task schema the manifest is written in, such as version = %q (this vibesys-cli reads %s)", ManifestName, Schema02.String(), SchemasText())
	}
	var v SchemaVersion
	switch x := raw.(type) {
	case string:
		var err error
		if v, err = ParseSchemaVersion(x); err != nil {
			return SchemaVersion{}, err
		}
	case int64:
		if x == 1 {
			return SchemaVersion{}, invalid("%s: version = 1 is the earlier spelling of the single-image format; write version = %q", ManifestName, Schema01.String())
		}
		return SchemaVersion{}, invalid("%s: version = %d is not a schema version: write it as MAJOR.MINOR in quotes, such as version = %q", ManifestName, x, Schema02.String())
	default:
		return SchemaVersion{}, invalid("%s: version must be a string MAJOR.MINOR, such as %q, got %v", ManifestName, Schema02.String(), raw)
	}
	for _, known := range Schemas() {
		if accepts(known, v) {
			return known, nil
		}
	}
	if newer(v) {
		return SchemaVersion{}, invalid("%s: version %q: this task needs task schema %s; this vibesys-cli reads %s; run `vibesys-cli upgrade`", ManifestName, v.String(), v, SchemasText())
	}
	return SchemaVersion{}, invalid("%s: version %q: task schema %s is not one this vibesys-cli reads (%s)", ManifestName, v.String(), v, SchemasText())
}

// accepts reports whether a reader of the schema known reads a file that declares v: exactly the
// version while the major version is 0, and the same major version up to its minor from 1.0 on.
func accepts(known, v SchemaVersion) bool {
	if known.Major == 0 {
		return v == known
	}
	return v.Major == known.Major && v.Minor <= known.Minor
}

// newer reports whether v is past every schema this build reads, so a newer build may read it.
func newer(v SchemaVersion) bool {
	for _, known := range Schemas() {
		if v.Major < known.Major || (v.Major == known.Major && v.Minor <= known.Minor) {
			return false
		}
	}
	return true
}
