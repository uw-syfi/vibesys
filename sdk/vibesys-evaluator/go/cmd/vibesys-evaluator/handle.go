package main

import (
	"fmt"
	"io"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// fail is a domain error of the given kind (the response's error_kind).
func fail(kind, format string, args ...any) error {
	return &DomainError{Kind: kind, Message: fmt.Sprintf(format, args...)}
}

// typed makes a Handler from a function of a request and a response message: the request is
// the proto3 JSON message on stdin (an empty stdin is the empty message; fields the build does
// not know are ignored), the response goes to stdout in the same encoding with the schema's
// field names.
func typed[Req, Resp any, PReq interface {
	*Req
	proto.Message
}, PResp interface {
	*Resp
	proto.Message
}](fn func(PReq) (PResp, error)) Handler {
	return func(rw IO) error {
		in, err := io.ReadAll(rw.Stdin)
		if err != nil {
			return err
		}
		req := PReq(new(Req))
		if len(in) > 0 {
			if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(in, req); err != nil {
				return fail("bad_request", "the request is not a valid message: %v", err)
			}
		}
		resp, err := fn(req)
		if err != nil {
			return err
		}
		out, err := protojson.MarshalOptions{UseProtoNames: true, EmitUnpopulated: true}.Marshal(resp)
		if err != nil {
			return err
		}
		_, err = rw.Stdout.Write(append(out, '\n'))
		return err
	}
}
