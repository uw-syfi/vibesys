.PHONY: all clean stack-candidate.so

TARGET_DIR := $(or $(CARGO_TARGET_DIR),target)

all: stack-candidate.so

stack-candidate.so: Cargo.toml Cargo.lock src/lib.rs src/ffi.rs
	cargo build --release --locked
	cp "$(TARGET_DIR)/release/libstack_candidate.$$(test "$$(uname -s)" = Darwin && printf dylib || printf so)" "$@"

clean:
	cargo clean
	rm -f stack-candidate.so
