// Package plan reads a requests file into a Plan. Every PlannedOperation is validated and
// compiled once when the file is read (templates split into literals and refs, refs resolved
// against earlier captures, the exclusive keys chained in plan order), so a plan loadgen cannot
// run fails before the phase starts. The plan then keeps only the file's bytes and a few words per
// operation; a sender compiles an operation again from its bytes when it takes it (no validation),
// so the memory a plan needs is about its file's size, whatever its operations compile to.
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

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/files"
)

// ErrInvalid means the requests file is not a plan this loadgen can run (exit status 2).
var ErrInvalid = errors.New("invalid plan")

// Plan is a whole requests file, ready to send.
type Plan struct {
	// Names are the operation names; Op.Operation indexes into them.
	Names []string

	format files.Format
	data   []byte
	// bounds[i] and bounds[i+1] delimit operation i's frame in data.
	bounds    []int64
	operation []uint32
	after     []int64
	gated     []bool
}

// Len is the number of planned operations.
func (p *Plan) Len() uint64 { return uint64(len(p.operation)) }

// Gated reports whether a later operation waits for operation index (it shares its key).
func (p *Plan) Gated(index uint64) bool { return p.gated[index] }

// Operation is operation index's number (an index into Names), without compiling it.
func (p *Plan) Operation(index uint64) uint32 { return p.operation[index] }

// Op compiles operation index from its bytes. Read compiled the same bytes already, so it cannot
// fail; a failure means the plan's memory changed under it and panics.
func (p *Plan) Op(index uint64) *Op {
	planned := &pb.PlannedOperation{}
	if err := files.Unmarshal(files.MessageOf(p.data[p.bounds[index]:p.bounds[index+1]], p.format), p.format, planned); err != nil {
		panic(fmt.Sprintf("plan: operation %d no longer decodes: %v", index, err))
	}
	op, err := compileOp(index, planned, p.Names)
	if err != nil {
		panic(fmt.Sprintf("plan: operation %d no longer compiles: %v", index, err))
	}
	op.After, op.Gated = p.after[index], p.gated[index]
	return &op
}

// Op is one compiled PlannedOperation.
type Op struct {
	// Index is the operation's position in the plan.
	Index      uint64
	Operation  uint32
	Steps      []Step
	KeepBodies bool
	// After is the index of the previous operation with the same exclusive key, or -1. The
	// operation may start only once that one has finished, which grants each key in plan order.
	After int64
	// Gated is true when a later operation waits for this one (it has a successor on its key).
	Gated bool
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

// Read reads a whole requests file. Errors wrap ErrInvalid or files.ErrSchemaVersion when the
// file is not a runnable plan, and are I/O errors otherwise.
func Read(in io.Reader, format files.Format) (*Plan, error) {
	data, err := io.ReadAll(in)
	if err != nil {
		return nil, err
	}
	return Parse(data, format)
}

// Parse reads a requests file's bytes, as Read does; the plan keeps data, which the caller must
// not change.
func Parse(data []byte, format files.Format) (*Plan, error) {
	frames, _, err := files.NewFrames(data, format, pb.FileKind_FILE_KIND_REQUESTS)
	if err != nil {
		return nil, invalid(err)
	}
	start, end, err := frames.Next()
	if errors.Is(err, io.EOF) {
		return nil, fmt.Errorf("%w: no RequestsHeader after the FileHeader", ErrInvalid)
	}
	header := &pb.RequestsHeader{}
	if err == nil {
		err = files.Unmarshal(frames.Message(start, end), format, header)
	}
	if err != nil {
		return nil, invalid(fmt.Errorf("RequestsHeader: %w", err))
	}
	if err := validator.Validate(header); err != nil {
		return nil, fmt.Errorf("%w: RequestsHeader: %v", ErrInvalid, err)
	}
	const preallocLimit = 1 << 22
	bounds := make([]int64, 0, min(header.GetCount(), preallocLimit)+1)
	for {
		start, end, err := frames.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, invalid(fmt.Errorf("operation %d: %w", len(bounds), err))
		}
		if len(bounds) == 0 {
			bounds = append(bounds, int64(start))
		}
		bounds = append(bounds, int64(end))
	}
	count := max(len(bounds)-1, 0)
	if uint64(count) != header.GetCount() {
		return nil, fmt.Errorf("%w: RequestsHeader.count is %d but the file holds %d operations", ErrInvalid, header.GetCount(), count)
	}
	p := &Plan{Names: header.GetOperationNames(), format: format, data: data, bounds: bounds}
	if err := p.compileAll(); err != nil {
		return nil, err
	}
	return p, nil
}

// New compiles a plan from messages, as Read does from a file.
func New(names []string, ops []*pb.PlannedOperation) (*Plan, error) {
	data, err := files.Append(nil, files.Binpb, files.Header(pb.FileKind_FILE_KIND_REQUESTS, "plan.New"))
	if err != nil {
		return nil, err
	}
	if data, err = files.Append(data, files.Binpb, &pb.RequestsHeader{OperationNames: names, Count: uint64(len(ops))}); err != nil {
		return nil, err
	}
	for _, op := range ops {
		if data, err = files.Append(data, files.Binpb, op); err != nil {
			return nil, fmt.Errorf("%w: operation: %v", ErrInvalid, err)
		}
	}
	return Parse(data, files.Binpb)
}

// compileAll validates and compiles every operation on every CPU (validation dominates reading a
// plan), keeping only each one's number and exclusive key, then chains the exclusive keys in plan
// order. The first invalid operation is reported.
func (p *Plan) compileAll() error {
	count := len(p.bounds) - 1
	if count < 0 {
		count = 0
	}
	p.operation = make([]uint32, count)
	keys := make([]string, count)
	errs := make([]error, count)
	workers := min(runtime.GOMAXPROCS(0), max(count/1024, 1))
	const chunk = 256
	var next atomic.Int64
	var wg sync.WaitGroup
	for range workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				from := int(next.Add(chunk) - chunk)
				if from >= count {
					return
				}
				for i := from; i < min(from+chunk, count); i++ {
					planned := &pb.PlannedOperation{}
					err := files.Unmarshal(files.MessageOf(p.data[p.bounds[i]:p.bounds[i+1]], p.format), p.format, planned)
					if err == nil {
						err = validator.Validate(planned)
					}
					if err == nil {
						_, err = compileOp(uint64(i), planned, p.Names)
					}
					errs[i] = err
					p.operation[i], keys[i] = planned.GetOperation(), planned.GetExclusiveKey()
				}
			}
		}()
	}
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			return fmt.Errorf("%w: operation %d: %v", ErrInvalid, i, err)
		}
	}
	p.after = make([]int64, count)
	p.gated = make([]bool, count)
	lastByKey := map[string]int64{}
	for i, key := range keys {
		p.after[i] = -1
		if key == "" {
			continue
		}
		if previous, ok := lastByKey[key]; ok {
			p.after[i] = previous
			p.gated[previous] = true
		}
		lastByKey[key] = int64(i)
	}
	return nil
}

func invalid(err error) error {
	if errors.Is(err, files.ErrSchemaVersion) || errors.Is(err, files.ErrMalformed) {
		return fmt.Errorf("%w: %w", ErrInvalid, err)
	}
	return err
}

// compileOp compiles one operation that validates (without its exclusive-key chain).
func compileOp(index uint64, planned *pb.PlannedOperation, names []string) (Op, error) {
	if int(planned.GetOperation()) >= len(names) {
		return Op{}, fmt.Errorf("operation index %d is outside operation_names (%d names)", planned.GetOperation(), len(names))
	}
	op := Op{Index: index, Operation: planned.GetOperation(), KeepBodies: planned.GetKeepBodies(), After: -1}
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
