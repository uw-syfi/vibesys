package workload

import (
	"fmt"
	"strings"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

// The schema's patterns, checked by hand: a regular expression per step showed in profiles.
const (
	namePattern    = `^[a-z][a-z0-9_]{0,62}$`
	pointerPattern = `^(/[^/]*)+$`
)

// validName reports whether name matches namePattern.
func validName(name string) bool {
	if len(name) < 1 || len(name) > 63 || name[0] < 'a' || name[0] > 'z' {
		return false
	}
	for i := 1; i < len(name); i++ {
		c := name[i]
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '_') {
			return false
		}
	}
	return true
}

// validPointer reports whether pointer matches pointerPattern: it starts with "/".
func validPointer(pointer string) bool { return strings.HasPrefix(pointer, "/") }

const (
	maxSteps     = 16
	maxCaptures  = 16
	maxExclusive = 256
)

// part is a literal text or, when ref is set, a captured value.
type part struct {
	literal string
	ref     string
}

func appendLiteral(out *[]part, text string) { *out = append(*out, part{literal: text}) }

// Template is a text made of literal parts and values captured by earlier steps of the same
// request (Ref): Concat("Bearer ", Ref("token")).
type Template struct{ parts []part }

// Text is what a path, query value, header value or form value may be: a string or a Template.
type Text interface{ ~string | Template }

// Ref is the value an earlier step of the same request captured as name (Capture). It panics on
// a name outside ^[a-z][a-z0-9_]{0,62}$.
func Ref(name string) Template {
	if !validName(name) {
		panic(fmt.Sprintf("workload.Ref name %q must match %s", name, namePattern))
	}
	return Template{parts: []part{{ref: name}}}
}

// Concat joins strings and Templates into one Template. It panics on any other argument.
func Concat(parts ...any) Template {
	var out []part
	for _, p := range parts {
		switch v := p.(type) {
		case string:
			appendLiteral(&out, v)
		case Template:
			out = append(out, v.parts...)
		default:
			panic(fmt.Sprintf("workload.Concat takes strings and Templates, got %T", p))
		}
	}
	return merged(out)
}

// merged joins adjacent literals and drops empty ones.
func merged(parts []part) Template {
	var out []part
	for _, p := range parts {
		switch {
		case p.ref == "" && len(out) > 0 && out[len(out)-1].ref == "":
			out[len(out)-1].literal += p.literal
		case p.ref != "" || p.literal != "":
			out = append(out, p)
		}
	}
	return Template{parts: out}
}

// String shows the template with its refs as {name}.
func (t Template) String() string {
	var b strings.Builder
	for _, p := range t.parts {
		if p.ref != "" {
			b.WriteString("{" + p.ref + "}")
		} else {
			b.WriteString(p.literal)
		}
	}
	return b.String()
}

func asTemplate[T Text](value T) (Template, bool) {
	switch v := any(value).(type) {
	case Template:
		return v, true
	default:
		return Template{parts: []part{{literal: fmt.Sprint(v)}}}, false
	}
}

// encoder turns texts into template messages and collects the refs they use.
type encoder struct{ refs map[string]bool }

func (e *encoder) template(t Template, literal bool, encoding pb.RefEncoding) *pb.Template {
	if literal {
		return literalTemplate(t.parts[0].literal)
	}
	parts := make([]*pb.Template_Part, 0, len(t.parts))
	for _, p := range t.parts {
		if p.ref != "" {
			e.refs[p.ref] = true
			parts = append(parts, &pb.Template_Part{Part: &pb.Template_Part_Ref{Ref: &pb.Ref{Name: p.ref, Encoding: encoding}}})
		} else {
			parts = append(parts, &pb.Template_Part{Part: &pb.Template_Part_Literal{Literal: p.literal}})
		}
	}
	if len(parts) == 0 {
		return literalTemplate("")
	}
	return &pb.Template{Parts: parts}
}

func literalTemplate(text string) *pb.Template {
	return &pb.Template{Parts: []*pb.Template_Part{{Part: &pb.Template_Part_Literal{Literal: text}}}}
}

// Step is one HTTP call of a request, built by Get, Post or Put with Options.
type Step struct {
	message  *pb.Step
	refs     map[string]bool
	captures []string
}

// String shows the method and path.
func (s Step) String() string {
	if s.message == nil {
		return "Step()"
	}
	call := s.message.GetHttp()
	method := strings.TrimPrefix(call.GetMethod().String(), "HTTP_METHOD_")
	var path strings.Builder
	for _, p := range call.GetPath().GetParts() {
		if ref := p.GetRef(); ref != nil {
			path.WriteString("{" + ref.GetName() + "}")
		} else {
			path.WriteString(p.GetLiteral())
		}
	}
	return "Step(" + method + " " + path.String() + ")"
}

// Option sets one part of a Step: Query, Header, Form, JSON, Body, Capture, Expect.
type Option func(*stepBuilder)

type stepBuilder struct {
	enc      encoder
	query    []*pb.Param
	headers  []*pb.Param
	form     []*pb.Param
	hasForm  bool
	json     []part
	hasJSON  bool
	body     []byte
	hasBody  bool
	captures []*pb.Capture
	names    []string
	expect   []uint32
}

func param[V Text](b *stepBuilder, name string, value V, where string) *pb.Param {
	if name == "" {
		panic(fmt.Sprintf("workload.%s: names are non-empty", where))
	}
	t, isTemplate := asTemplate(value)
	return &pb.Param{Name: name, Value: b.enc.template(t, !isTemplate, pb.RefEncoding_REF_ENCODING_RAW)}
}

// Query adds a query parameter, URL-encoded by loadgen.
func Query[V Text](name string, value V) Option {
	return func(b *stepBuilder) { b.query = append(b.query, param(b, name, value, "Query")) }
}

// Header adds a request header.
func Header[V Text](name string, value V) Option {
	return func(b *stepBuilder) { b.headers = append(b.headers, param(b, name, value, "Header")) }
}

// Form adds a field of a URL-encoded form body.
func Form[V Text](name string, value V) Option {
	return func(b *stepBuilder) {
		b.form, b.hasForm = append(b.form, param(b, name, value, "Form")), true
	}
}

// JSON sets a JSON body (Content-Type: application/json unless a Header sets one), encoded as
// the Python SDK encodes its json argument: compact, non-ASCII escaped, an Object or struct in
// field order, a map with its keys sorted; a Template inside becomes a JSON string with the
// captured value inserted.
func JSON(value any) Option {
	return func(b *stepBuilder) {
		var parts []part
		jsonParts(value, &parts, "json")
		b.json, b.hasJSON = merged(parts).parts, true
	}
}

// Body sets a body sent as is.
func Body(body []byte) Option {
	return func(b *stepBuilder) { b.body, b.hasBody = body, true }
}

// Capture takes a value from the step's response by JSON pointer (such as "/token"), for later
// steps of the request to send with Ref(name).
func Capture(name, pointer string) Option {
	return func(b *stepBuilder) {
		if !validName(name) {
			panic(fmt.Sprintf("workload.Capture name %q must match %s", name, namePattern))
		}
		if !validPointer(pointer) {
			panic(fmt.Sprintf("workload.Capture %q: %q is not a JSON pointer such as \"/token\"", name, pointer))
		}
		b.captures = append(b.captures, &pb.Capture{Name: name, Source: &pb.Capture_JsonPointer{JsonPointer: pointer}})
		b.names = append(b.names, name)
	}
}

// Expect sets the statuses that count as success (200 when not set).
func Expect(statuses ...int) Option {
	return func(b *stepBuilder) {
		if len(statuses) == 0 || len(statuses) > maxSteps {
			panic(fmt.Sprintf("workload.Expect: 1 to %d statuses, got %v", maxSteps, statuses))
		}
		b.expect = b.expect[:0]
		for _, s := range statuses {
			if s < 100 || s > 599 {
				panic(fmt.Sprintf("workload.Expect: HTTP statuses are in 100..599, got %d", s))
			}
			b.expect = append(b.expect, uint32(s))
		}
		if len(b.expect) == 1 && b.expect[0] == 200 {
			b.expect = nil // empty means 200 only
		}
	}
}

// Get is a GET of path (starting with "/") below the target.
func Get[P Text](path P, options ...Option) Step {
	return step(pb.HttpMethod_HTTP_METHOD_GET, path, options)
}

// Post is a POST of path (starting with "/") below the target, with at most one of Form, JSON
// and Body.
func Post[P Text](path P, options ...Option) Step {
	return step(pb.HttpMethod_HTTP_METHOD_POST, path, options)
}

// Put is a PUT of path (starting with "/") below the target; its options are Post's.
func Put[P Text](path P, options ...Option) Step {
	return step(pb.HttpMethod_HTTP_METHOD_PUT, path, options)
}

func step[P Text](method pb.HttpMethod, path P, options []Option) Step {
	b := &stepBuilder{enc: encoder{refs: map[string]bool{}}}
	t, isTemplate := asTemplate(path)
	if len(t.parts) == 0 || t.parts[0].ref != "" || !strings.HasPrefix(t.parts[0].literal, "/") {
		panic(fmt.Sprintf("workload: path %q must start with a literal \"/\"", t.String()))
	}
	call := &pb.HttpCall{Method: method, Path: b.enc.template(t, !isTemplate, pb.RefEncoding_REF_ENCODING_RAW)}
	for _, option := range options {
		option(b)
	}
	bodies := 0
	for _, has := range []bool{b.hasForm, b.hasJSON, b.hasBody} {
		if has {
			bodies++
		}
	}
	if bodies > 1 {
		panic("workload: pass at most one of Form, JSON and Body")
	}
	if len(b.captures) > maxCaptures {
		panic(fmt.Sprintf("workload: at most %d captures per step", maxCaptures))
	}
	call.Query, call.Headers = b.query, b.headers
	switch {
	case b.hasForm:
		call.Body = &pb.HttpCall_Form{Form: &pb.Form{Fields: b.form}}
	case b.hasJSON:
		if len(b.json) == 1 && b.json[0].ref == "" {
			call.Body = &pb.HttpCall_Raw{Raw: []byte(b.json[0].literal)}
		} else {
			call.Body = &pb.HttpCall_Text{Text: b.enc.template(Template{parts: b.json}, false, pb.RefEncoding_REF_ENCODING_JSON_STRING)}
		}
		hasType := false
		for _, h := range b.headers {
			hasType = hasType || strings.EqualFold(h.GetName(), "content-type")
		}
		if !hasType {
			call.Headers = append(call.Headers, &pb.Param{Name: "Content-Type", Value: literalTemplate("application/json")})
		}
	case b.hasBody:
		call.Body = &pb.HttpCall_Raw{Raw: b.body}
	}
	return Step{
		message:  &pb.Step{Call: &pb.Step_Http{Http: call}, Captures: b.captures, ExpectStatus: b.expect},
		refs:     b.enc.refs,
		captures: b.names,
	}
}

// Request is what one operation sends: Steps in order on one client, with one latency.
//
// Info describes what the request was built from (a JSON object: a map, a struct or an Object;
// nil is {}); it is not sent, and comes back as JSON in the Call that Check and Apply receive.
// Requests with the same non-empty Exclusive key (at most 256 bytes) never run at the same time,
// and run in stream order.
type Request struct {
	Steps     []Step
	Info      any
	Exclusive string
}

// planned is the stream message of request as operation number operation.
func planned(request Request, operation int, keepBodies bool) (*pb.PlannedOperation, error) {
	if len(request.Steps) < 1 || len(request.Steps) > maxSteps {
		return nil, fmt.Errorf("Request takes 1 to %d steps, got %d", maxSteps, len(request.Steps))
	}
	if len(request.Exclusive) > maxExclusive {
		return nil, fmt.Errorf("Request Exclusive is at most %d bytes, got %d", maxExclusive, len(request.Exclusive))
	}
	captured := map[string]bool{}
	steps := make([]*pb.Step, len(request.Steps))
	for n, s := range request.Steps {
		if s.message == nil {
			return nil, fmt.Errorf("Request step %d is empty (build steps with Get, Post or Put)", n)
		}
		var missing []string
		for ref := range s.refs {
			if !captured[ref] {
				missing = append(missing, ref)
			}
		}
		if len(missing) > 0 {
			return nil, fmt.Errorf("Request step %d refers to %v that no earlier step captures", n, missing)
		}
		for _, name := range s.captures {
			captured[name] = true
		}
		steps[n] = s.message
	}
	return &pb.PlannedOperation{
		Operation:    uint32(operation),
		Steps:        steps,
		ExclusiveKey: request.Exclusive,
		KeepBodies:   keepBodies,
	}, nil
}
