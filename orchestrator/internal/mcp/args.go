package mcp

import (
	"bytes"
	"encoding/json"
	"errors"
	"math"
	"sort"
	"strconv"
	"strings"
	"sync"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	"github.com/uw-syfi/vibesys/orchestrator/internal/names"
)

// What is wrong with an argument, as the template that words it tells them apart.
const (
	problemUnknown  = "unknown"  // no such argument
	problemType     = "type"     // not the kind of value the argument takes
	problemEnum     = "enum"     // not one of the values the argument takes
	problemRequired = "required" // missing, or empty where a value is needed
	problemGroup    = "group"    // not exactly one of a group of arguments
	problemRule     = "rule"     // breaks another rule of the message
)

// problem is one thing wrong with a call's arguments. Field is the argument's path, such as
// worker.candidate.commit; the other fields are the data its template needs.
type problem struct {
	Kind, Field string
	Want        string // the kind of value, or the list of arguments or values it takes
	Got         string // the value given, for an enum
	Detail      string // the rule's own words
	Doc         string // what a group is
}

// argumentsError is the refusal of a call whose arguments are wrong. It lists every problem, so
// that the agent fixes the call once.
type argumentsError struct {
	tool     string
	problems []problem
}

func (e *argumentsError) Error() string {
	return say("arguments", struct {
		Tool     string
		Problems []problem
	}{e.tool, e.problems})
}

// readInput reads the arguments of a call into into, the message that is the tool's input. A key
// the message does not offer is refused, so is a value of the wrong kind, and so is a message that
// breaks the protovalidate rules written in its proto.
func readInput(tool string, args json.RawMessage, into proto.Message) error {
	var probs []problem
	var raw map[string]any
	dec := json.NewDecoder(bytes.NewReader(args))
	dec.UseNumber()
	if err := dec.Decode(&raw); err != nil || raw == nil {
		return &argumentsError{tool, []problem{{Kind: problemType, Field: "arguments", Want: "an object"}}}
	}
	fill(into.ProtoReflect(), raw, "", &probs)
	if len(probs) > 0 {
		return &argumentsError{tool, probs}
	}
	return nil
}

// checkInput holds a read message to the rules of its proto.
func checkInput(tool string, in proto.Message) error {
	err := validator().Validate(in)
	if err == nil {
		return nil
	}
	var verr *protovalidate.ValidationError
	if !errors.As(err, &verr) {
		return &argumentsError{tool, []problem{{Kind: problemRule, Detail: err.Error()}}}
	}
	md := in.ProtoReflect().Descriptor()
	var probs []problem
	for _, v := range verr.Violations {
		probs = append(probs, violation(md, protovalidate.FieldPathString(v.Proto.GetField()), v.Proto.GetRuleId(), v.Proto.GetMessage()))
	}
	return &argumentsError{tool, probs}
}

var (
	validatorOnce sync.Once
	theValidator  protovalidate.Validator
)

func validator() protovalidate.Validator {
	validatorOnce.Do(func() {
		v, err := protovalidate.New()
		if err != nil {
			panic("mcp: the validator cannot be built: " + err.Error())
		}
		theValidator = v
	})
	return theValidator
}

// violation turns a broken rule into a problem that says what to give instead.
func violation(md protoreflect.MessageDescriptor, path, rule, message string) problem {
	fd, parent := resolve(md, path)
	switch {
	case fd == nil && parent != nil: // a group of which one argument is to be given.
		for _, g := range groups(parent) {
			return problem{Kind: problemGroup, Field: path, Want: strings.Join(g.Fields, ", "), Doc: g.Doc}
		}
	case fd != nil && fd.Kind() == protoreflect.EnumKind && strings.HasPrefix(rule, "enum"):
		return problem{Kind: problemEnum, Field: path, Want: names.Join(allowedSpellings(fd))}
	case fd != nil && (rule == "required" || strings.HasPrefix(rule, "string.min_len")):
		return problem{Kind: problemRequired, Field: path, Detail: fieldDoc(fd)}
	}
	return problem{Kind: problemRule, Field: path, Detail: message}
}

// resolve finds the field a path names. For a path that ends at a one-of group, which has no field,
// it returns no field and the message that holds the group.
func resolve(md protoreflect.MessageDescriptor, path string) (protoreflect.FieldDescriptor, protoreflect.MessageDescriptor) {
	var fd protoreflect.FieldDescriptor
	cur := md
	for _, part := range strings.Split(path, ".") {
		if cur == nil {
			return nil, nil
		}
		if part == "" {
			return nil, cur
		}
		fd = cur.Fields().ByName(protoreflect.Name(part))
		if fd == nil {
			return nil, cur
		}
		cur = fd.Message()
	}
	return fd, nil
}

// fill sets the fields of m from a decoded JSON object, and reports what it cannot set.
func fill(m protoreflect.Message, obj map[string]any, prefix string, probs *[]problem) {
	md := m.Descriptor()
	keys := make([]string, 0, len(obj))
	for k := range obj {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	given := map[protoreflect.Name]bool{}
	for _, key := range keys {
		fd := md.Fields().ByName(protoreflect.Name(key))
		path := prefix + key
		if fd == nil || hidden(fd) {
			*probs = append(*probs, problem{Kind: problemUnknown, Field: path, Want: strings.Join(offered(md), ", ")})
			continue
		}
		given[fd.Name()] = true
		set(m, fd, obj[key], path, probs)
	}
	for _, g := range groups(md) {
		n := 0
		for _, f := range g.Fields {
			if given[protoreflect.Name(f)] {
				n++
			}
		}
		if n > 1 {
			*probs = append(*probs, problem{Kind: problemGroup, Field: strings.TrimSuffix(prefix, "."), Want: strings.Join(g.Fields, ", "), Doc: g.Doc})
		}
	}
}

func offered(md protoreflect.MessageDescriptor) []string {
	var out []string
	for i := 0; i < md.Fields().Len(); i++ {
		if fd := md.Fields().Get(i); !hidden(fd) {
			out = append(out, string(fd.Name()))
		}
	}
	return out
}

func set(m protoreflect.Message, fd protoreflect.FieldDescriptor, v any, path string, probs *[]problem) {
	wrong := func(want string) { *probs = append(*probs, problem{Kind: problemType, Field: path, Want: want}) }
	switch fd.Kind() {
	case protoreflect.StringKind:
		s, ok := v.(string)
		if !ok {
			wrong("a string")
			return
		}
		m.Set(fd, protoreflect.ValueOfString(s))
	case protoreflect.BoolKind:
		b, ok := v.(bool)
		if !ok {
			wrong("true or false")
			return
		}
		m.Set(fd, protoreflect.ValueOfBool(b))
	case protoreflect.Uint32Kind, protoreflect.Uint64Kind:
		n, ok := wholeNumber(v)
		if !ok || (fd.Kind() == protoreflect.Uint32Kind && n > math.MaxUint32) {
			wrong("a whole number, zero or more")
			return
		}
		if fd.Kind() == protoreflect.Uint32Kind {
			m.Set(fd, protoreflect.ValueOfUint32(uint32(n)))
		} else {
			m.Set(fd, protoreflect.ValueOfUint64(n))
		}
	case protoreflect.EnumKind:
		s, ok := v.(string)
		if !ok {
			wrong("a string")
			return
		}
		n, found := names.Lookup(fd.Enum(), s)
		if !found {
			*probs = append(*probs, problem{Kind: problemEnum, Field: path, Got: s, Want: names.Join(allowedSpellings(fd))})
			return
		}
		m.Set(fd, protoreflect.ValueOfEnum(n))
	case protoreflect.MessageKind:
		obj, ok := v.(map[string]any)
		if !ok {
			wrong("an object")
			return
		}
		fill(m.Mutable(fd).Message(), obj, path+".", probs)
	default:
		wrong("a value this tool does not take")
	}
}

func wholeNumber(v any) (uint64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	u, err := strconv.ParseUint(n.String(), 10, 64)
	return u, err == nil
}
