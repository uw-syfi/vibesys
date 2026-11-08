package tomlproto_test

import (
	"fmt"
	"math"
	"math/rand/v2"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protodesc"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/reflect/protoregistry"
	"google.golang.org/protobuf/types/descriptorpb"
	"google.golang.org/protobuf/types/dynamicpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/tomlproto"
)

// testSchema is a schema of major version 1, as task schemas are from 1.0 on: each field says
// the minor version that added it. It exists only here, to hold the decoder to the rule before any
// real schema reaches 1.0.
//
//	message TestManifestV1 {
//	  option (task_schema) = {major: 1 minor: 3};
//	  string version = 1 [(task_schema_since_minor) = 0];
//	  string name = 2 [(task_schema_since_minor) = 0];
//	  uint32 workers = 3 [(task_schema_since_minor) = 2];
//	  Nested nested = 4 [(task_schema_since_minor) = 3];
//	}
//	message Nested { string note = 1 [(task_schema_since_minor) = 3]; }
func testSchema(t *testing.T) protoreflect.MessageDescriptor {
	t.Helper()
	since := func(minor uint32) *descriptorpb.FieldOptions {
		o := &descriptorpb.FieldOptions{}
		proto.SetExtension(o, cliv1.E_TaskSchemaSinceMinor, minor)
		return o
	}
	field := func(name string, n int32, typ descriptorpb.FieldDescriptorProto_Type, minor uint32, typeName string) *descriptorpb.FieldDescriptorProto {
		f := &descriptorpb.FieldDescriptorProto{
			Name: proto.String(name), Number: proto.Int32(n), Type: typ.Enum(), JsonName: proto.String(name),
			Label: descriptorpb.FieldDescriptorProto_LABEL_OPTIONAL.Enum(), Options: since(minor),
		}
		if typeName != "" {
			f.TypeName = proto.String(typeName)
		}
		return f
	}
	msgOpts := &descriptorpb.MessageOptions{}
	proto.SetExtension(msgOpts, cliv1.E_TaskSchema, &cliv1.TaskSchema{Major: 1, Minor: 3})
	file := &descriptorpb.FileDescriptorProto{
		Name: proto.String("tomlproto_test/schema.proto"), Package: proto.String("tomlproto.test"), Syntax: proto.String("proto3"),
		MessageType: []*descriptorpb.DescriptorProto{
			{
				Name: proto.String("TestManifestV1"), Options: msgOpts,
				Field: []*descriptorpb.FieldDescriptorProto{
					field("version", 1, descriptorpb.FieldDescriptorProto_TYPE_STRING, 0, ""),
					field("name", 2, descriptorpb.FieldDescriptorProto_TYPE_STRING, 0, ""),
					field("workers", 3, descriptorpb.FieldDescriptorProto_TYPE_UINT32, 2, ""),
					field("nested", 4, descriptorpb.FieldDescriptorProto_TYPE_MESSAGE, 3, ".tomlproto.test.Nested"),
				},
			},
			{
				Name:  proto.String("Nested"),
				Field: []*descriptorpb.FieldDescriptorProto{field("note", 1, descriptorpb.FieldDescriptorProto_TYPE_STRING, 3, "")},
			},
		},
	}
	fd, err := protodesc.NewFile(file, protoregistry.GlobalFiles)
	if err != nil {
		t.Fatal(err)
	}
	return fd.Messages().ByName("TestManifestV1")
}

func decode(t *testing.T, md protoreflect.MessageDescriptor, text string, declared *tomlproto.Version) (*dynamicpb.Message, error) {
	t.Helper()
	m := dynamicpb.NewMessage(md)
	return m, tomlproto.Decode([]byte(text), m, tomlproto.Options{Declared: declared})
}

func TestTheSchemaOptionIsReadFromTheMessage(t *testing.T) {
	md := testSchema(t)
	if v, ok := tomlproto.Schema(md); !ok || v != (tomlproto.Version{Major: 1, Minor: 3}) {
		t.Fatalf("schema %v %v", v, ok)
	}
	if since, ok := tomlproto.Since(md.Fields().ByName("workers")); !ok || since != 2 {
		t.Fatalf("since %d %v", since, ok)
	}
	if _, ok := tomlproto.Since(md.Fields().ByName("nested").Message().Fields().ByName("note")); !ok {
		t.Fatal("a nested field has no since")
	}
}

// A key is read when the declared minor version is at least the one that added it, and refused
// otherwise, naming the key and the version it needs, at any depth.
func TestAKeyNewerThanTheDeclaredMinorIsRefusedNamingTheVersionItNeeds(t *testing.T) {
	md := testSchema(t)
	keys := []struct {
		text  string
		key   string
		since uint32
	}{
		{`name = "a"`, "name", 0},
		{`workers = 4`, "workers", 2},
		{"[nested]", "nested", 3},
		{"[nested]\nnote = \"n\"", "nested", 3},
	}
	rng := rand.New(rand.NewPCG(3, 4))
	for range 200 {
		k := keys[rng.IntN(len(keys))]
		minor := uint32(rng.IntN(5))
		declared := tomlproto.Version{Major: 1, Minor: minor}
		_, err := decode(t, md, k.text, &declared)
		if minor >= k.since {
			if err != nil {
				t.Fatalf("%q at 1.%d: %v", k.text, minor, err)
			}
			continue
		}
		want := fmt.Sprintf("the key %q needs task schema 1.%d", k.key, k.since)
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Fatalf("%q at 1.%d: %v, want %q", k.text, minor, err, want)
		}
	}
	// Nothing declared, nothing is refused for its version.
	if _, err := decode(t, md, "workers = 1\n[nested]\nnote = \"x\"", nil); err != nil {
		t.Fatal(err)
	}
}

func TestUnknownKeysAndWrongTypesAreRefusedByName(t *testing.T) {
	md := testSchema(t)
	cases := map[string]string{
		"colour = 1":                      `unknown key "colour"`,
		"[nested]\ncolour = 1":            `unknown key "nested.colour"`,
		"Name = \"a\"":                    `unknown key "Name"`,
		"workers = \"four\"":              "workers",
		"workers = -1":                    "workers",
		"nested = 3":                      "nested",
		"name = \"a\"\nname = \"b\"":      "name",
		"this is not toml":                "",
		"[nested]\nnote = [\"a\", \"b\"]": "nested.note",
	}
	for text, says := range cases {
		_, err := decode(t, md, text, nil)
		if err == nil || !strings.Contains(err.Error(), says) {
			t.Errorf("%q: %v, want it to name %q", text, err, says)
		}
	}
	m, err := decode(t, md, "name = \"a\"\nworkers = 4\n[nested]\nnote = \"n\"", nil)
	if err != nil {
		t.Fatal(err)
	}
	if got := m.Get(md.Fields().ByName("workers")).Uint(); got != 4 {
		t.Fatalf("workers %d", got)
	}
}

// TOML has nan and inf, JSON does not: a file that writes one is decoded to the value, and the
// message's own rules decide whether the field may hold it, naming the key.
func TestNonFiniteNumbersDecodeToTheValueInsteadOfFailingWithoutAKey(t *testing.T) {
	for text, check := range map[string]func(float64) bool{
		"relative_noise = nan":  math.IsNaN,
		"relative_noise = inf":  func(f float64) bool { return math.IsInf(f, 1) },
		"relative_noise = -inf": func(f float64) bool { return math.IsInf(f, -1) },
	} {
		var m cliv1.TaskPareto
		if err := tomlproto.Decode([]byte(text), &m, tomlproto.Options{}); err != nil || !check(m.GetRelativeNoise()) {
			t.Errorf("%q: %v, %v", text, m.GetRelativeNoise(), err)
		}
	}
}

// A section held as data is a google.protobuf.Struct, which is JSON, and JSON has no nan or
// infinity. A non-finite number in one cannot be represented, so it is refused naming its key, the
// way an unknown key or a wrong type is, instead of reaching json.Marshal, whose error names
// neither the key nor the section.
func TestANonFiniteNumberInADataSectionIsRefusedNamingItsKey(t *testing.T) {
	cases := map[string]string{
		"[resources]\nx = nan":          "resources.x",
		"[resources]\nx = inf":          "resources.x",
		"[resources]\nx = -inf":         "resources.x",
		"[resources.limits]\nz = nan":   "resources.limits.z",
		"[resources]\narr = [1.0, nan]": "resources.arr[1]",
		"[workspace]\ny = -inf":         "workspace.y",
	}
	for text, says := range cases {
		var m cliv1.TaskManifestV02
		err := tomlproto.Decode([]byte(text), &m, tomlproto.Options{})
		if err == nil || !strings.Contains(err.Error(), says) {
			t.Errorf("%q: %v, want it to name %q", text, err, says)
		}
	}
}

// A data section of finite values still decodes to the struct it did before.
func TestAFiniteDataSectionDecodesToItsValues(t *testing.T) {
	var m cliv1.TaskManifestV02
	if err := tomlproto.Decode([]byte("[resources]\ngpus = 4\nname = \"a100\"\nratio = 0.5"), &m, tomlproto.Options{}); err != nil {
		t.Fatal(err)
	}
	got := m.GetResources().AsMap()
	if got["gpus"] != float64(4) || got["name"] != "a100" || got["ratio"] != 0.5 {
		t.Errorf("resources %v", got)
	}
}

// A TOML integer is 64-bit, so a value too big (or too small) for a 32-bit field is refused here,
// naming the key the file spells, instead of reaching protojson, which names the field in its JSON
// form and a position in the marshaled JSON, not the file. A 64-bit field holds any TOML integer.
func TestAnIntegerTooBigForItsFieldIsRefusedNamingItsKey(t *testing.T) {
	for _, c := range []struct {
		msg  proto.Message
		text string
		says string
	}{
		// uint32 fields of the task manifest (result_protocol and timeout_seconds).
		{&cliv1.TaskManifestV02{}, "[benchmark]\nresult_protocol = 5000000000", "benchmark.result_protocol"},
		{&cliv1.TaskManifestV02{}, "[benchmark]\ntimeout_seconds = 4294967296", "benchmark.timeout_seconds"},
		{&cliv1.TaskManifestV02{}, "[benchmark]\nresult_protocol = 99999999999999999", "benchmark.result_protocol"},
		// int32 field of the operator config (slurm.cpus), over and under its range.
		{&cliv1.OperatorConfig{}, "[slurm]\ncpus = 3000000000", "slurm.cpus"},
		{&cliv1.OperatorConfig{}, "[slurm]\ncpus = -3000000000", "slurm.cpus"},
	} {
		err := tomlproto.Decode([]byte(c.text), c.msg, tomlproto.Options{})
		if err == nil || !strings.Contains(err.Error(), c.says) || strings.Contains(err.Error(), "proto:") {
			t.Errorf("%q: %v, want it to name %q and not be a raw protojson error", c.text, err, c.says)
		}
	}
}

// A value that fits its field's width is still read, at both bounds, for signed and unsigned.
func TestAnIntegerThatFitsItsFieldIsRead(t *testing.T) {
	var over cliv1.TaskManifestV02
	if err := tomlproto.Decode([]byte("[benchmark]\nresult_protocol = 4294967295"), &over, tomlproto.Options{}); err != nil {
		t.Errorf("the largest uint32: %v", err)
	}
	var cpus cliv1.OperatorConfig
	if err := tomlproto.Decode([]byte("[slurm]\ncpus = 2147483647"), &cpus, tomlproto.Options{}); err != nil {
		t.Errorf("the largest int32: %v", err)
	}
	if got := cpus.GetSlurm().GetCpus(); got != 2147483647 {
		t.Errorf("cpus = %d, want the largest int32", got)
	}
}
