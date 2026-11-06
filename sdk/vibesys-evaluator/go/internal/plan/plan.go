// Package plan compiles PlannedOperations into Ops ready to send: validated, templates split into
// literals and refs, refs resolved against earlier captures, exclusive keys chained in stream
// order. Stream does it while a phase runs, from a generator's request stream, ahead of the
// senders; Read does it for a finite stream read whole (loadgen probe's ready steps).
package plan

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"

	"buf.build/go/protovalidate"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/wire"
)

// ErrInvalid means a request stream holds an operation this loadgen cannot run (exit status 2).
var ErrInvalid = errors.New("invalid request stream")

// Plan is a finite request stream read whole (loadgen probe's ready steps).
type Plan struct {
	// Names are the operation names; Op.Operation indexes into them.
	Names []string
	Ops   []Op
}

// Op is one compiled PlannedOperation.
type Op struct {
	// Index is the operation's position in the plan.
	Index      uint64
	Operation  uint32
	Steps      []Step
	KeepBodies bool
	// Key is the exclusive key ("" for none).
	Key string
	// Prev is closed when the previous operation with the same exclusive key has finished (nil
	// when there is none); the operation may start only then, which grants each key in stream
	// order. Done is this operation's own (nil without a key): close it when it has finished.
	Prev <-chan struct{}
	Done chan struct{}
}

// Step is one compiled HTTP call.
type Step struct {
	Method   string
	Path     Text
	Query    []Param
	Headers  []Param
	Body     Body
	Captures []Capture
	// Expect holds the statuses that count as success; never empty.
	Expect []uint32
}

// Param is a query parameter, header or form field.
type Param struct {
	Name  string
	Value Text
}

// BodyKind says which body a step sends.
type BodyKind int

const (
	// NoBody sends no body.
	NoBody BodyKind = iota
	// RawBody sends Body.Raw as is.
	RawBody
	// TextBody sends Body.Text filled.
	TextBody
	// FormBody sends Body.Form URL-encoded.
	FormBody
)

// Body is a step's request body.
type Body struct {
	Kind BodyKind
	Raw  []byte
	Text Text
	Form []Param
}

// Capture takes one value from a step's response: by JSON pointer (Pointer non-nil) or header.
type Capture struct {
	Name    string
	Pointer []string
	Header  string
}

var validator = mustValidator()

func mustValidator() protovalidate.Validator {
	v, err := protovalidate.New()
	if err != nil {
		panic(err)
	}
	return v
}

// Read reads a whole finite request stream (to its end). Errors wrap ErrInvalid or
// files.ErrSchemaVersion when it is not runnable, and are I/O errors otherwise. Its exclusive keys
// are not chained: a probe sends one operation at a time.
func Read(in io.Reader, format files.Format) (*Plan, error) {
	reader, err := newReader(in, format)
	if err != nil {
		return nil, err
	}
	var ops []*pb.PlannedOperation
	for {
		planned := &pb.PlannedOperation{}
		err := reader.read.Read(planned)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, invalid(fmt.Errorf("operation %d: %w", len(ops), err))
		}
		ops = append(ops, planned)
	}
	return compileAll(reader.names, ops)
}

// header is a request stream's start: its operation names, and a reader of what follows.
type header struct {
	names []string
	read  *files.Reader
}

func newReader(in io.Reader, format files.Format) (header, error) {
	reader, _, err := files.NewReader(in, format, pb.FileKind_FILE_KIND_REQUESTS)
	if err != nil {
		return header{}, invalid(err)
	}
	h := &pb.RequestsHeader{}
	if err := reader.Read(h); err != nil {
		if errors.Is(err, io.EOF) {
			return header{}, fmt.Errorf("%w: no RequestsHeader after the FileHeader", ErrInvalid)
		}
		return header{}, invalid(fmt.Errorf("RequestsHeader: %w", err))
	}
	// The names first, in words the workload's author can act on: the generator's first message
	// is where a bad one shows, before any load.
	if err := wire.CheckOperationNames(h.GetOperationNames()); err != nil {
		return header{}, fmt.Errorf("%w: RequestsHeader.operation_names: %v", ErrInvalid, err)
	}
	if err := validator.Validate(h); err != nil {
		return header{}, fmt.Errorf("%w: RequestsHeader: %v", ErrInvalid, err)
	}
	return header{names: h.GetOperationNames(), read: reader}, nil
}

// New compiles operations from messages, as Read does from a stream.
func New(names []string, ops []*pb.PlannedOperation) (*Plan, error) {
	if err := validator.Validate(&pb.RequestsHeader{OperationNames: names}); err != nil {
		return nil, fmt.Errorf("%w: RequestsHeader: %v", ErrInvalid, err)
	}
	return compileAll(names, ops)
}

// compileAll validates and compiles every operation on every CPU (validation dominates
// compiling). The first invalid operation is reported.
func compileAll(names []string, planned []*pb.PlannedOperation) (*Plan, error) {
	p := &Plan{Names: names, Ops: make([]Op, len(planned))}
	errs := make([]error, len(planned))
	workers := min(runtime.GOMAXPROCS(0), max(len(planned)/1024, 1))
	var next atomic.Int64
	var wg sync.WaitGroup
	for range workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				i := int(next.Add(1) - 1)
				if i >= len(planned) {
					return
				}
				p.Ops[i], errs[i] = compile(uint64(i), planned[i], names)
			}
		}()
	}
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			return nil, fmt.Errorf("%w: operation %d: %v", ErrInvalid, i, err)
		}
	}
	return p, nil
}

func invalid(err error) error {
	if errors.Is(err, files.ErrSchemaVersion) || errors.Is(err, files.ErrMalformed) {
		return fmt.Errorf("%w: %w", ErrInvalid, err)
	}
	return err
}

func compile(index uint64, planned *pb.PlannedOperation, names []string) (Op, error) {
	if err := validator.Validate(planned); err != nil {
		return Op{}, err
	}
	if int(planned.GetOperation()) >= len(names) {
		return Op{}, fmt.Errorf("operation index %d is outside operation_names (%d names)", planned.GetOperation(), len(names))
	}
	op := Op{Index: index, Operation: planned.GetOperation(), KeepBodies: planned.GetKeepBodies(), Key: planned.GetExclusiveKey()}
	captured := map[string]bool{}
	for stepIndex, step := range planned.GetSteps() {
		compiled, err := compileStep(step, captured)
		if err != nil {
			return Op{}, fmt.Errorf("step %d: %w", stepIndex, err)
		}
		op.Steps = append(op.Steps, compiled)
		for _, capture := range compiled.Captures {
			captured[capture.Name] = true
		}
	}
	return op, nil
}

var methods = map[pb.HttpMethod]string{
	pb.HttpMethod_HTTP_METHOD_GET:    http.MethodGet,
	pb.HttpMethod_HTTP_METHOD_POST:   http.MethodPost,
	pb.HttpMethod_HTTP_METHOD_PUT:    http.MethodPut,
	pb.HttpMethod_HTTP_METHOD_DELETE: http.MethodDelete,
	pb.HttpMethod_HTTP_METHOD_PATCH:  http.MethodPatch,
}

func compileStep(step *pb.Step, captured map[string]bool) (Step, error) {
	call := step.GetHttp()
	if call == nil {
		return Step{}, errors.New("only HTTP calls are supported (Capabilities.calls)")
	}
	method, ok := methods[call.GetMethod()]
	if !ok {
		return Step{}, fmt.Errorf("unsupported HTTP method %s", call.GetMethod())
	}
	compiled := Step{Method: method, Expect: step.GetExpectStatus()}
	if len(compiled.Expect) == 0 {
		compiled.Expect = []uint32{200}
	}
	var err error
	if compiled.Path, err = compileText(call.GetPath(), captured); err != nil {
		return Step{}, fmt.Errorf("path: %w", err)
	}
	if !compiled.Path.StartsWithLiteralSlash() {
		return Step{}, errors.New("path must start with a literal \"/\"")
	}
	if compiled.Query, err = compileParams(call.GetQuery(), captured); err != nil {
		return Step{}, fmt.Errorf("query: %w", err)
	}
	if compiled.Headers, err = compileParams(call.GetHeaders(), captured); err != nil {
		return Step{}, fmt.Errorf("headers: %w", err)
	}
	switch body := call.GetBody().(type) {
	case nil:
	case *pb.HttpCall_Raw:
		compiled.Body = Body{Kind: RawBody, Raw: body.Raw}
	case *pb.HttpCall_Text:
		text, err := compileText(body.Text, captured)
		if err != nil {
			return Step{}, fmt.Errorf("text body: %w", err)
		}
		compiled.Body = Body{Kind: TextBody, Text: text}
	case *pb.HttpCall_Form:
		form, err := compileParams(body.Form.GetFields(), captured)
		if err != nil {
			return Step{}, fmt.Errorf("form: %w", err)
		}
		compiled.Body = Body{Kind: FormBody, Form: form}
	default:
		return Step{}, fmt.Errorf("unsupported body %T", body)
	}
	for _, capture := range step.GetCaptures() {
		switch source := capture.GetSource().(type) {
		case *pb.Capture_JsonPointer:
			pointer, err := ParsePointer(source.JsonPointer)
			if err != nil {
				return Step{}, fmt.Errorf("capture %q: %w", capture.GetName(), err)
			}
			compiled.Captures = append(compiled.Captures, Capture{Name: capture.GetName(), Pointer: pointer})
		case *pb.Capture_Header:
			compiled.Captures = append(compiled.Captures, Capture{Name: capture.GetName(), Header: source.Header})
		default:
			return Step{}, fmt.Errorf("capture %q has no source", capture.GetName())
		}
	}
	return compiled, nil
}

func compileParams(params []*pb.Param, captured map[string]bool) ([]Param, error) {
	compiled := make([]Param, 0, len(params))
	for _, param := range params {
		value, err := compileText(param.GetValue(), captured)
		if err != nil {
			return nil, fmt.Errorf("%q: %w", param.GetName(), err)
		}
		compiled = append(compiled, Param{Name: param.GetName(), Value: value})
	}
	return compiled, nil
}

// ParsePointer splits an RFC 6901 JSON pointer ("/a/0/b~1c") into its unescaped tokens.
func ParsePointer(pointer string) ([]string, error) {
	if !strings.HasPrefix(pointer, "/") {
		return nil, fmt.Errorf("JSON pointer %q must start with \"/\"", pointer)
	}
	tokens := strings.Split(pointer[1:], "/")
	for i, token := range tokens {
		for j := 0; j < len(token); j++ {
			if token[j] == '~' && (j+1 == len(token) || (token[j+1] != '0' && token[j+1] != '1')) {
				return nil, fmt.Errorf("JSON pointer %q has an invalid escape", pointer)
			}
		}
		tokens[i] = strings.ReplaceAll(strings.ReplaceAll(token, "~1", "/"), "~0", "~")
	}
	return tokens, nil
}
