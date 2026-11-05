module github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen

go 1.25.0

toolchain go1.25.9

require (
	buf.build/gen/go/bufbuild/protovalidate/protocolbuffers/go v1.36.9-20250912141014-52f32327d4b0.1
	google.golang.org/protobuf v1.36.12
)

tool google.golang.org/protobuf/cmd/protoc-gen-go
