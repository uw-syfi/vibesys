package archcheck_test

import (
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protodesc"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/reflect/protoregistry"
	"google.golang.org/protobuf/types/descriptorpb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/archcheck"
)

// The contract as built: no service of the orchestrator can carry a stored fact.
func TestNoServiceCanCarryAStoredFact(t *testing.T) {
	files := []protoreflect.FileDescriptor{
		pb.File_vibesys_orchestrator_v1_service_proto,
		pb.File_vibesys_orchestrator_v1_host_control_proto,
		pb.File_vibesys_orchestrator_v1_agent_session_proto,
	}
	for _, file := range files {
		if file.Services().Len() == 0 {
			t.Fatalf("%s has no service: this check walks nothing there", file.Path())
		}
	}
	if leaks := archcheck.StoredLeaks(files); len(leaks) > 0 {
		t.Fatal(strings.Join(leaks, "\n"))
	}
}

// Every private fact is marked, so the check and a reader of stored.proto agree on what they are.
func TestThePrivatePartOfARecordIsMarked(t *testing.T) {
	for _, m := range []proto.Message{&pb.AgentRecord{}, &pb.RunRecord{}, &pb.ReceiptRecord{}} {
		fields := m.ProtoReflect().Descriptor().Fields()
		for i := range fields.Len() {
			fd := fields.Get(i)
			marked := proto.GetExtension(fd.Options(), pb.E_StoredOnly).(bool)
			if public := fd.Number() == 1; marked == public {
				t.Errorf("%s: marked stored_only is %v; a record's field 1 is its public message and the rest are private", fd.FullName(), marked)
			}
		}
	}
}

func field(name string, number int32, kind descriptorpb.FieldDescriptorProto_Type, typeName string, marked bool) *descriptorpb.FieldDescriptorProto {
	f := &descriptorpb.FieldDescriptorProto{
		Name: proto.String(name), Number: proto.Int32(number), Type: kind.Enum(),
		Label: descriptorpb.FieldDescriptorProto_LABEL_OPTIONAL.Enum(),
	}
	if typeName != "" {
		f.TypeName = proto.String(typeName)
	}
	if marked {
		f.Options = &descriptorpb.FieldOptions{}
		proto.SetExtension(f.Options, pb.E_StoredOnly, true)
	}
	return f
}

func message(name string, fields ...*descriptorpb.FieldDescriptorProto) *descriptorpb.DescriptorProto {
	return &descriptorpb.DescriptorProto{Name: proto.String(name), Field: fields}
}

// A contract with one service. Its request holds Public; its response holds whatever is given, one
// message down, so that "transitively" is what is checked.
func contract(t *testing.T, responseField *descriptorpb.FieldDescriptorProto, extra ...*descriptorpb.FileDescriptorProto) protoreflect.FileDescriptor {
	t.Helper()
	files := &protoregistry.Files{}
	for _, dep := range []protoreflect.FileDescriptor{descriptorpb.File_google_protobuf_descriptor_proto, pb.File_vibesys_orchestrator_v1_stored_options_proto} {
		if err := files.RegisterFile(dep); err != nil {
			t.Fatal(err)
		}
	}
	deps := []string{}
	for _, f := range extra {
		built, err := protodesc.NewFile(f, files)
		if err != nil {
			t.Fatal(err)
		}
		if err := files.RegisterFile(built); err != nil {
			t.Fatal(err)
		}
		deps = append(deps, f.GetName())
	}
	file := &descriptorpb.FileDescriptorProto{
		Name: proto.String("test/service.proto"), Package: proto.String("t"), Syntax: proto.String("proto3"),
		Dependency: append(deps, pb.File_vibesys_orchestrator_v1_stored_options_proto.Path()),
		MessageType: []*descriptorpb.DescriptorProto{
			message("Request"),
			message("Response", field("nested", 1, descriptorpb.FieldDescriptorProto_TYPE_MESSAGE, ".t.Nested", false)),
			message("Nested", responseField),
		},
		Service: []*descriptorpb.ServiceDescriptorProto{{
			Name: proto.String("S"),
			Method: []*descriptorpb.MethodDescriptorProto{{
				Name: proto.String("Call"), InputType: proto.String(".t.Request"), OutputType: proto.String(".t.Response"),
			}},
		}},
	}
	built, err := protodesc.NewFile(file, files)
	if err != nil {
		t.Fatal(err)
	}
	return built
}

func TestAStoredFactOnAServicesMessageIsNamed(t *testing.T) {
	stored := &descriptorpb.FileDescriptorProto{
		Name: proto.String(archcheck.StoredFile), Package: proto.String("t"), Syntax: proto.String("proto3"),
		MessageType: []*descriptorpb.DescriptorProto{message("Secret")},
	}
	elsewhere := &descriptorpb.FileDescriptorProto{
		Name: proto.String("test/public.proto"), Package: proto.String("t"), Syntax: proto.String("proto3"),
		MessageType: []*descriptorpb.DescriptorProto{message("Secret")},
	}
	str := descriptorpb.FieldDescriptorProto_TYPE_STRING
	msg := descriptorpb.FieldDescriptorProto_TYPE_MESSAGE
	cases := []struct {
		name string
		file protoreflect.FileDescriptor
		want string
	}{
		{"a clean contract", contract(t, field("note", 1, str, "", false)), ""},
		{"a public message of another file", contract(t, field("note", 1, msg, ".t.Secret", false), elsewhere), ""},
		{"a marked field", contract(t, field("token", 1, str, "", true)), "t.S.Call.nested.token is marked stored_only"},
		{"a message of the stored file", contract(t, field("note", 1, msg, ".t.Secret", false), stored), "t.S.Call.nested.note reaches t.Secret"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := strings.Join(archcheck.StoredLeaks([]protoreflect.FileDescriptor{c.file}), "\n")
			if c.want == "" && got != "" || !strings.Contains(got, c.want) {
				t.Fatalf("leaks %q, want %q", got, c.want)
			}
		})
	}
}
