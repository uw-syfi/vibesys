module example.com/go-bench

go 1.25.0

require github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go v0.0.0

require (
	buf.build/gen/go/bufbuild/protovalidate/protocolbuffers/go v1.36.9-20250912141014-52f32327d4b0.1 // indirect
	google.golang.org/protobuf v1.36.12 // indirect
)

// A task pins a release instead (github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go
// vX.Y.Z); the example builds against the SDK in this repository.
replace github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go => ../../go
