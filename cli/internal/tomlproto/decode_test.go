package tomlproto_test

import (
	"fmt"
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
