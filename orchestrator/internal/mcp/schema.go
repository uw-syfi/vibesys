package mcp

import (
	"fmt"
	"slices"

	"buf.build/gen/go/bufbuild/protovalidate/protocolbuffers/go/buf/validate"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/names"
)

// inputSchema is the JSON Schema of a tool's arguments, built from the message that is its input:
// its fields are the properties, the words an agent reads are the tool_field_doc options, and the
// rules in the message's protovalidate options say which are required and which values are allowed.
// It uses only plain keywords, with no oneOf or anyOf, which a client may refuse at the top of a
// tool's schema: a group of which one field is to be given is said in a description instead, and
// the rule is held by the validation of a call.
func inputSchema(md protoreflect.MessageDescriptor) map[string]any {
	props := map[string]any{}
	required := []string{}
	fields := md.Fields()
	for i := 0; i < fields.Len(); i++ {
		fd := fields.Get(i)
		if hidden(fd) {
			continue
		}
		props[string(fd.Name())] = propertySchema(fd)
		if mustBeGiven(fd) {
			required = append(required, string(fd.Name()))
		}
	}
	schema := map[string]any{"type": "object", "properties": props, "required": required, "additionalProperties": false}
	if doc := oneofSentences(md); doc != "" {
		schema["description"] = doc
	}
	return schema
}

func propertySchema(fd protoreflect.FieldDescriptor) map[string]any {
	var p map[string]any
	switch fd.Kind() {
	case protoreflect.StringKind:
		p = map[string]any{"type": "string"}
		if rules := fieldRules(fd); rules.GetString().GetMinLen() > 0 {
			p["minLength"] = rules.GetString().GetMinLen()
		}
	case protoreflect.BoolKind:
		p = map[string]any{"type": "boolean"}
	case protoreflect.Uint32Kind, protoreflect.Uint64Kind:
		p = map[string]any{"type": "integer", "minimum": 0}
	case protoreflect.EnumKind:
		p = map[string]any{"type": "string", "enum": allowedSpellings(fd)}
	case protoreflect.MessageKind:
		p = inputSchema(fd.Message())
		if doc := oneofSentences(fd.Message()); doc != "" {
			p["description"] = fieldDoc(fd) + " " + doc
			return p
		}
	default:
		// A field of another kind is not an argument an agent can give. The build of the tool table
		// refuses it, so a message cannot become a tool that quietly omits a field.
		panic(fmt.Sprintf("mcp: field %s has kind %s, which a tool argument cannot be", fd.FullName(), fd.Kind()))
	}
	p["description"] = fieldDoc(fd)
	return p
}

// allowedSpellings lists the values an enum field takes: its values but the unspecified one and
// those its rules refuse.
func allowedSpellings(fd protoreflect.FieldDescriptor) []string {
	var skip []protoreflect.EnumNumber
	for _, n := range fieldRules(fd).GetEnum().GetNotIn() {
		skip = append(skip, protoreflect.EnumNumber(n))
	}
	return names.Spellings(fd.Enum(), skip...)
}

// mustBeGiven says a field has no valid empty value: the rules require it, or refuse the empty
// string, or refuse the unspecified enum value.
func mustBeGiven(fd protoreflect.FieldDescriptor) bool {
	rules := fieldRules(fd)
	if rules.GetRequired() || rules.GetString().GetMinLen() > 0 {
		return true
	}
	return fd.Kind() == protoreflect.EnumKind && slices.Contains(rules.GetEnum().GetNotIn(), 0)
}

func fieldRules(fd protoreflect.FieldDescriptor) *validate.FieldRules {
	rules, _ := proto.GetExtension(fd.Options(), validate.E_Field).(*validate.FieldRules)
	return rules
}

func fieldDoc(fd protoreflect.FieldDescriptor) string {
	doc, _ := proto.GetExtension(fd.Options(), pb.E_ToolFieldDoc).(string)
	return doc
}

func hidden(fd protoreflect.FieldDescriptor) bool {
	h, _ := proto.GetExtension(fd.Options(), pb.E_ToolFieldHidden).(bool)
	return h
}

// groups are the one-of groups of a message whose fields are arguments, with the fields an agent
// is offered.
type group struct {
	Fields []string
	Doc    string
}

func groups(md protoreflect.MessageDescriptor) []group {
	var out []group
	oneofs := md.Oneofs()
	for i := 0; i < oneofs.Len(); i++ {
		od := oneofs.Get(i)
		if od.IsSynthetic() {
			continue
		}
		g := group{}
		g.Doc, _ = proto.GetExtension(od.Options(), pb.E_ToolOneofDoc).(string)
		for j := 0; j < od.Fields().Len(); j++ {
			if fd := od.Fields().Get(j); !hidden(fd) {
				g.Fields = append(g.Fields, string(fd.Name()))
			}
		}
		out = append(out, g)
	}
	return out
}

func oneofSentences(md protoreflect.MessageDescriptor) string {
	var out string
	for _, g := range groups(md) {
		if out != "" {
			out += " "
		}
		out += say("oneof", g)
	}
	return out
}
