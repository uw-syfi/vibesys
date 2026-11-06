// Package messages constructs schema messages tersely, for tests, fixtures and the selftest plan.
package messages

import (
	"bytes"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
)

// Part is a template piece: a string literal or a Ref.
type Part any

// Ref is a template reference to a captured value.
type Ref struct {
	Name     string
	Encoding pb.RefEncoding
}

// Raw is a ref inserted as is.
func Raw(name string) Ref { return Ref{Name: name, Encoding: pb.RefEncoding_REF_ENCODING_RAW} }

// Text builds a template from literals and refs.
func Text(parts ...Part) *pb.Template {
	template := &pb.Template{}
	for _, part := range parts {
		switch value := part.(type) {
		case string:
			template.Parts = append(template.Parts, &pb.Template_Part{Part: &pb.Template_Part_Literal{Literal: value}})
		case Ref:
			template.Parts = append(template.Parts, &pb.Template_Part{Part: &pb.Template_Part_Ref{Ref: &pb.Ref{Name: value.Name, Encoding: value.Encoding}}})
		}
	}
	return template
}

// HTTP builds a step.
func HTTP(method pb.HttpMethod, path ...Part) *pb.Step {
	return &pb.Step{Call: &pb.Step_Http{Http: &pb.HttpCall{Method: method, Path: Text(path...)}}}
}

// Get builds a GET step.
func Get(path ...Part) *pb.Step { return HTTP(pb.HttpMethod_HTTP_METHOD_GET, path...) }

// Post builds a POST step with a text body.
func Post(body *pb.Template, path ...Part) *pb.Step {
	step := HTTP(pb.HttpMethod_HTTP_METHOD_POST, path...)
	step.GetHttp().Body = &pb.HttpCall_Text{Text: body}
	return step
}

// Header adds a header to a step.
func Header(step *pb.Step, name string, value ...Part) *pb.Step {
	step.GetHttp().Headers = append(step.GetHttp().Headers, &pb.Param{Name: name, Value: Text(value...)})
	return step
}

// Query adds a query parameter to a step.
func Query(step *pb.Step, name string, value ...Part) *pb.Step {
	step.GetHttp().Query = append(step.GetHttp().Query, &pb.Param{Name: name, Value: Text(value...)})
	return step
}

// CaptureJSON adds a JSON-pointer capture to a step.
func CaptureJSON(step *pb.Step, name, pointer string) *pb.Step {
	step.Captures = append(step.Captures, &pb.Capture{Name: name, Source: &pb.Capture_JsonPointer{JsonPointer: pointer}})
	return step
}

// Expect sets a step's expected statuses.
func Expect(step *pb.Step, statuses ...uint32) *pb.Step {
	step.ExpectStatus = statuses
	return step
}

// Op builds a planned operation.
func Op(operation uint32, steps ...*pb.Step) *pb.PlannedOperation {
	return &pb.PlannedOperation{Operation: operation, Steps: steps}
}

// Exclusive sets an operation's exclusive key.
func Exclusive(op *pb.PlannedOperation, key string) *pb.PlannedOperation {
	op.ExclusiveKey = key
	return op
}

// RequestsFile encodes a whole finite request stream.
func RequestsFile(format files.Format, names []string, ops ...*pb.PlannedOperation) []byte {
	return RequestsFileVersion(format, files.SchemaVersion, names, ops...)
}

// RequestsFileVersion encodes a finite request stream whose header claims a schema version.
func RequestsFileVersion(format files.Format, version uint32, names []string, ops ...*pb.PlannedOperation) []byte {
	var out bytes.Buffer
	header := files.Header(pb.FileKind_FILE_KIND_REQUESTS, "build")
	header.SchemaVersion = version
	writer, err := files.NewWriter(&out, format, header)
	must(err)
	must(writer.Write(&pb.RequestsHeader{OperationNames: names}))
	for _, op := range ops {
		must(writer.Write(op))
	}
	must(writer.Flush())
	return out.Bytes()
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
