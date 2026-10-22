// Package hostoperations owns the generated request pairing and execution budgets.
package hostoperations

import (
	"context"
	"fmt"
	"time"

	"buf.build/go/protovalidate"
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

const MaxPayload = 1 << 20

type Operation struct {
	ID, RequestTag, ResponseTag uint32
	Name                        string
	Cap, Grace, FirstEvent      time.Duration
	Effect                      hostv1.OperationEffect
	Retry                       hostv1.Retry
	Executor                    hostv1.Executor
	Cardinality                 hostv1.Cardinality
	NewRequest, NewResponse     func() proto.Message
	Correlations                [][2][]string
}

// Lookup rejects unknown routing keys; the returned descriptor is independent.
func Lookup(id uint32) (Operation, error) {
	op, ok := registry[id]
	if !ok {
		return Operation{}, fmt.Errorf("unknown operation id: %d", id)
	}
	op.Correlations = clonePaths(op.Correlations)
	return op, nil
}

func clonePaths(paths [][2][]string) [][2][]string {
	result := make([][2][]string, len(paths))
	for i, pair := range paths {
		for j, path := range pair {
			result[i][j] = append([]string(nil), path...)
		}
	}
	return result
}

// Decode validates hostile boundary bytes before an executor sees a request.
func (op Operation) Decode(payload []byte) (proto.Message, error) {
	if len(payload) > MaxPayload {
		return nil, fmt.Errorf("%s: request exceeds 1 MiB", op.Name)
	}
	request := op.NewRequest()
	if err := proto.Unmarshal(payload, request); err != nil {
		return nil, err
	}
	if err := knownFields(request.ProtoReflect()); err != nil {
		return nil, err
	}
	if err := protovalidate.Validate(request); err != nil {
		return nil, err
	}
	return request, nil
}

// ValidateResponse checks pairing, schema and correlation before delivery.
func (op Operation) ValidateResponse(request, response proto.Message) error {
	if request == nil || response == nil || !request.ProtoReflect().IsValid() || !response.ProtoReflect().IsValid() {
		return fmt.Errorf("%s: missing operation message", op.Name)
	}
	if request.ProtoReflect().Descriptor().FullName() != op.NewRequest().ProtoReflect().Descriptor().FullName() || response.ProtoReflect().Descriptor().FullName() != op.NewResponse().ProtoReflect().Descriptor().FullName() {
		return fmt.Errorf("%s: wrong response method", op.Name)
	}
	if err := knownFields(response.ProtoReflect()); err != nil {
		return err
	}
	if err := protovalidate.Validate(response); err != nil {
		return err
	}
	for _, pair := range op.Correlations {
		if value(request.ProtoReflect(), pair[0]) != value(response.ProtoReflect(), pair[1]) {
			return fmt.Errorf("%s: response correlation mismatch", op.Name)
		}
	}
	return nil
}

func value(message protoreflect.Message, path []string) any {
	for i, name := range path {
		field := message.Descriptor().Fields().ByName(protoreflect.Name(name))
		v := message.Get(field)
		if i == len(path)-1 {
			if field.Kind() == protoreflect.BytesKind {
				return string(v.Bytes())
			}
			return v.Interface()
		}
		message = v.Message()
	}
	return nil
}

// Execute owns one bounded unary invocation. A shell supplies the executor;
// this table determines both decoders and the deadline, never a second switch.
func Execute(ctx context.Context, id uint32, payload []byte, invoke func(context.Context, Operation, proto.Message) (proto.Message, error)) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	op, err := Lookup(id)
	if err != nil {
		return nil, err
	}
	if op.Cardinality != hostv1.Cardinality_CARDINALITY_UNARY {
		return nil, fmt.Errorf("%s: unary executor cannot execute stream", op.Name)
	}
	request, err := op.Decode(payload)
	if err != nil {
		return nil, err
	}
	bounded, cancel := context.WithTimeout(ctx, op.Cap)
	defer cancel()
	original := proto.Clone(request)
	response, err := invoke(bounded, op, request)
	if err != nil {
		return nil, err
	}
	if err := bounded.Err(); err != nil {
		return nil, err
	}
	if err := op.ValidateResponse(original, response); err != nil {
		return nil, err
	}
	output, err := proto.Marshal(response)
	if len(output) > MaxPayload {
		return nil, fmt.Errorf("%s: response exceeds 1 MiB", op.Name)
	}
	return output, err
}

func knownFields(message protoreflect.Message) error {
	if !message.IsValid() {
		return fmt.Errorf("missing operation message")
	}
	if len(message.GetUnknown()) != 0 {
		return fmt.Errorf("%s: unknown fields", message.Descriptor().FullName())
	}
	var result error
	message.Range(func(field protoreflect.FieldDescriptor, value protoreflect.Value) bool {
		switch {
		case field.IsMap():
			if field.MapValue().Message() != nil {
				value.Map().Range(func(_ protoreflect.MapKey, entry protoreflect.Value) bool {
					result = knownFields(entry.Message())
					return result == nil
				})
			}
		case field.IsList():
			if field.Message() != nil {
				for i := 0; i < value.List().Len() && result == nil; i++ {
					result = knownFields(value.List().Get(i).Message())
				}
			}
		case field.Message() != nil:
			result = knownFields(value.Message())
		}
		return result == nil
	})
	return result
}
