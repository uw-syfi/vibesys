# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
User space stack allocator with support of multi-buffer.

The class is implemented to run in a NKI enviornment.

"""

from dataclasses import dataclass
from typing import Optional

import nki.language as nl

from .kernel_assert import kernel_assert
from .logging import Logger, get_logger
from .tree_logger import TreeLogger


def sizeinbytes(dtype):
    if str(dtype) == str(nl.float32):
        return 4
    elif (
        str(dtype) == str(nl.bfloat16)
        or str(dtype) == str(nl.float16)
        or str(dtype) == str(nl.uint16)
        or str(dtype) == str(nl.int16)
    ):
        return 2
    elif (
        str(dtype) == str(nl.int8)
        or str(dtype) == str(nl.uint8)
        or str(dtype) in ["float8e4", "float8_e4m3", "float8e5", "float8_e5m2"]
    ):
        return 1
    elif str(dtype) == str(nl.int32) or str(dtype) == str(nl.uint32):
        return 4
    elif str(dtype) == str(nl.float4_e2m1fn_x4):
        return 2
    elif str(dtype) == str(nl.float8_e4m3fn_x4) or str(dtype) == str(nl.float8_e5m2_x4):
        return 4
    kernel_assert(False, f"dtype size unknown! {dtype}")


def align_to(value, alignment):
    # This function is copied from the llvm::alignTo
    return ((value + alignment - 1) // alignment) * alignment


def num_elts(shape):
    res = 1
    for i in range(len(shape)):
        res = res * shape[i]
    return res


@dataclass
class Scope(nl.NKIObject):
    starting_addr: int
    # number of independent sections in each stack frame,
    # used for multibuffer
    num_sections: int
    name: str
    cur_section_id: int = 0


class SbufManager(nl.NKIObject):
    def __init__(
        self,
        sb_lower_bound: int,
        sb_upper_bound: int,
        logger: Optional[Logger] = None,
        use_auto_alloc: bool = False,
        default_stack_alloc: bool = True,
    ):
        """
        Creates a SbufManager (referred to as SBM) instance
        with lower and upper bound, which jointly defines the contiguous region in sbuf
        that the SBUF manager may use.

        The Stack would grow upwards from the lower_bound, while the heap would grow downwards
        from the upper_bound.

        :param lower_bound: lower bound of the available sbuf memory region.
        :param upper_bound: upper bound of the available sbuf memory region.
        :param use_auto_alloc: Whether to use auto-allocation. Defaults to False.
        """
        self.lower_bound = sb_lower_bound
        self.upper_bound = sb_upper_bound
        self.stack_curr_addr = sb_lower_bound
        self.heap_curr_addr = sb_upper_bound
        self.use_auto_alloc = use_auto_alloc
        self.default_stack_alloc = default_stack_alloc
        self.logger = logger
        if self.logger == None:
            self.logger = get_logger("SBM")
        self.scopes = []
        self.heap = []
        self.heap_names = []
        self.tree_logger = TreeLogger("SBM", self.logger)

        # Stats tracking
        self.max_stack_usage = 0
        self.max_heap_usage = 0
        self.max_combined_usage = 0
        self.total_stack_allocs = 0
        self.total_heap_allocs = 0

        # Log initialization
        total_size = sb_upper_bound - sb_lower_bound
        self.logger.info(f"SBM initialized: range=[{sb_lower_bound}, {sb_upper_bound}), size={total_size} B")
        self.logger.debug(
            f"SBM config: auto_alloc={use_auto_alloc}, default_stack={default_stack_alloc}, "
            f"stack_start={sb_lower_bound}, heap_start={sb_upper_bound}"
        )
        self.prefix = ''

    def _get_prefixed_name(self, name):
        """Apply prefix to tensor name."""
        if name is None:
            return None
        return f"{self.prefix}{name}"

    def _update_stats(self):
        stack_usage = self.stack_curr_addr - self.lower_bound
        heap_usage = self.upper_bound - self.heap_curr_addr
        combined = stack_usage + heap_usage
        if stack_usage > self.max_stack_usage:
            self.max_stack_usage = stack_usage
        if heap_usage > self.max_heap_usage:
            self.max_heap_usage = heap_usage
        if combined > self.max_combined_usage:
            self.max_combined_usage = combined

    def _print_stats(self):
        total = self.upper_bound - self.lower_bound
        free = total - self.max_combined_usage
        pct = (self.max_combined_usage * 100) // total
        self.logger.info(
            f"[SBM] SB memory statistics: max_usage={self.max_combined_usage} B ({pct}%), free={free} B, stack={self.max_stack_usage} B ({self.total_stack_allocs} allocs), heap={self.max_heap_usage} B ({self.total_heap_allocs} allocs)"
        )

    def is_auto_alloc(self):
        return self.use_auto_alloc

    def is_default_stack_alloc(self):
        return self.default_stack_alloc

    def is_default_heap_alloc(self):
        return not self.default_stack_alloc

    def open_scope(self, interleave_degree=1, name=""):
        """
        Add a new frame on the stack. SBUF addresses allocated on the stack will be
        automatically freed when its creation scope is closed.

        The optional argument `interleave_degree` helps manage multi-buffering in a loop.
        See the documentation of `increment_section` for more information.
        The optional argument `name` helps identify scopes in debug logging.
        """
        self.scopes.append(Scope(self.stack_curr_addr, interleave_degree, name))
        scope_name = f"'{name}'" if name else "(unnamed)"
        self.tree_logger.log(
            f"▶ SCOPE {scope_name} [interleave={interleave_degree}] @ {self.stack_curr_addr}",
            len(self.scopes) - 1,
            is_scope_boundary=True,
        )

    def increment_section(self):
        """
        Increment the section count in the current scope. If the current section count reached the
        interleave_degree of the current scope, the address is reset to the beginning address of
        the current scope. Otherwise, continue allocate on the current address.

        Example:
        sbm = SbufManager(0, 128*1024, get_logger())
        sbm.open_scope(interleave_degree=2)

        for i in range(4):
          sbm.alloc_stack((128, 128), dtype=nl.bfloat16)
          sbm.increment_section()
        sbm.close_scope()

        In the example above, the sbm would emit the following address for the 4 allocations,
                      address.      section_id after increment_section is called()
        Iteration 0:     0                     1
        Iteration 1:     256                   0
        Iteration 2:     0                     1
        Iteration 3:     256                   0

        This generally used to control multi-buffering in loops.
        """
        top_scope = self.scopes[-1]
        top_scope.cur_section_id = top_scope.cur_section_id + 1
        if top_scope.cur_section_id == top_scope.num_sections:
            top_scope.cur_section_id = 0
            self.stack_curr_addr = top_scope.starting_addr
            self.tree_logger.log(f"↻ section: 0/{top_scope.num_sections} @ {self.stack_curr_addr}", len(self.scopes))
        else:
            self.tree_logger.log(
                f"↳ section: {top_scope.cur_section_id}/{top_scope.num_sections} @ {self.stack_curr_addr}",
                len(self.scopes),
            )

    def close_scope(self):
        """
        Close the current stack scope. All tensors alloated within the scope will be freed
        """
        closing_scope = self.scopes[-1]
        freed_bytes = self.stack_curr_addr - closing_scope.starting_addr
        self.stack_curr_addr = closing_scope.starting_addr
        self.scopes.pop()
        scope_name = f"'{closing_scope.name}'" if closing_scope.name else "(unnamed)"
        self.tree_logger.log(f"◀ END {scope_name} freed={freed_bytes} B", len(self.scopes), is_scope_boundary=True)
        # Auto-flush when last scope is closed
        if not self.scopes:
            self.tree_logger.flush()
            self._print_stats()

    def alloc(self, shape, dtype, buffer=nl.sbuf, name=None, base_partition=0, align=None):
        """
        Allocate a tensor on the stack or the heap, depending on default allocation type.

        :param shape: shape of the tensor to be allocated
        :param dtype: dtype of the tensor to be allocated
        :param buffer: type of the buffer, currently only nl.sbuf is supported
        :param name: name of the tensor. Must be unique in the kernel
        :param base_partition: The base partition of the allocation, default to 0
        :param align: Alignment requirement of the address.
        :param is_stack: Whether to allocate on stack or heap.

        :return: a sbuf tensor described above.
        """
        if self.default_stack_alloc:
            return self.alloc_stack(
                shape,
                dtype,
                buffer=buffer,
                name=name,
                base_partition=base_partition,
                align=align,
            )
        else:
            return self.alloc_heap(
                shape,
                dtype,
                buffer=buffer,
                name=name,
                base_partition=base_partition,
                align=align,
            )

    def alloc_stack(self, shape, dtype, buffer=nl.sbuf, name=None, base_partition=0, align=None):
        """
        Allocate a tensor on the stack which will be automatically freed when a scope closes.
        This method would raise an error if there are no open scope.

        :param shape: shape of the tensor to be allocated
        :param dtype: dtype of the tensor to be allocated
        :param buffer: type of the buffer, currently only nl.sbuf is supported
        :param name: name of the tensor. Must be unique in the kernel
        :param base_partition: The base partition of the allocation, default to 0
        :param align: Alignment requirement of the address.
        :return: a sbuf tensor described above.
        """
        kernel_assert(buffer == nl.sbuf, "alloc_stack is only supported for SBUF tensors")
        N = num_elts(shape[1:])
        bytes_per_partition = N * sizeinbytes(dtype)

        if not self.is_auto_alloc() and self.stack_curr_addr + bytes_per_partition > self.heap_curr_addr:
            available = self.heap_curr_addr - self.stack_curr_addr
            self.logger.error(f"Stack OOM: requested={bytes_per_partition} B, available={available} B")
            self.logger.debug(
                f"Allocation failure: name={name}, shape={shape}, dtype={dtype}, size={bytes_per_partition} B, "
                f"stack_addr={self.stack_curr_addr}, heap_addr={self.heap_curr_addr}, "
                f"range=[{self.lower_bound}, {self.upper_bound})"
            )
            self.tree_logger.flush()
            self._print_stats()
            kernel_assert(False, "Stack out of memory")

        if not self.scopes:
            self.logger.error("Cannot allocate in stack without an open scope")
            self.logger.debug(f"Stack state: no open scopes, stack_addr={self.stack_curr_addr}")
            kernel_assert(False, "Cannot allocate in stack without an open scope")

        if align == None:
            align = sizeinbytes(dtype)
        self.stack_curr_addr = align_to(self.stack_curr_addr, align)
        tensor_name = self._get_prefixed_name(name)
        if self.use_auto_alloc:
            mloc = nl.ndarray(shape=shape, dtype=dtype, buffer=buffer, name=tensor_name)
        else:
            mloc = nl.ndarray(
                shape=shape,
                dtype=dtype,
                buffer=buffer,
                name=tensor_name,
                address=(base_partition, self.stack_curr_addr),
            )
        addr_start = self.stack_curr_addr
        self.stack_curr_addr = self.stack_curr_addr + bytes_per_partition
        self.total_stack_allocs = self.total_stack_allocs + 1
        self._update_stats()
        self.tree_logger.log(
            f"{tensor_name or '(unnamed)'}: {bytes_per_partition} B @ {addr_start} {shape} {dtype}", len(self.scopes)
        )
        self.logger.debug(
            f"Stack allocation: name={name}, shape={shape}, dtype={dtype}, size={bytes_per_partition} B, "
            f"addr_range=[{addr_start}, {self.stack_curr_addr}), "
            f"partition={base_partition}, align={align}, scope_depth={len(self.scopes)}, "
            f"free_space={self.heap_curr_addr - self.stack_curr_addr} B"
        )

        return mloc

    def alloc_heap(self, shape, dtype, buffer=nl.sbuf, name=None, base_partition=0, align=None):
        """
        Allocate a tensor on the heap.
        It will not be automatically freed when a scope closes,
        and must be released manually using pop_heap().

        :param shape: shape of the tensor to be allocated
        :param dtype: dtype of the tensor to be allocated
        :param buffer: type of the buffer, currently only nl.sbuf is supported
        :param name: name of the tensor. Must be unique in the kernel
        :param base_partition: The base partition of the allocation, default to 0
        :return: a sbuf tensor described above.
        """
        kernel_assert(buffer == nl.sbuf, "alloc_heap is only supported for SBUF tensors")
        N = num_elts(shape[1:])
        bytes_per_partition = N * sizeinbytes(dtype)

        if not self.is_auto_alloc() and self.stack_curr_addr + bytes_per_partition > self.heap_curr_addr:
            available = self.heap_curr_addr - self.stack_curr_addr
            self.logger.error(f"Heap OOM: requested={bytes_per_partition} B, available={available} B")
            self.logger.debug(
                f"Allocation failure: name={name}, shape={shape}, dtype={dtype}, size={bytes_per_partition} B, "
                f"stack_addr={self.stack_curr_addr}, heap_addr={self.heap_curr_addr}, "
                f"range=[{self.lower_bound}, {self.upper_bound})"
            )
            self.tree_logger.flush()
            self._print_stats()
            kernel_assert(False, "Heap out of memory")

        if align != None:
            self.heap_curr_addr = align_to(self.heap_curr_addr, align)
        base_addr = self.heap_curr_addr - bytes_per_partition
        self.heap_curr_addr -= bytes_per_partition
        self.heap_curr_addr = align_to(self.heap_curr_addr - 3, 4)  # heap grows down, so should the align

        tensor_name = self._get_prefixed_name(name)
        if self.use_auto_alloc:
            mloc = nl.ndarray(shape=shape, dtype=dtype, buffer=buffer, name=tensor_name)
        else:
            mloc = nl.ndarray(
                shape=shape,
                dtype=dtype,
                buffer=buffer,
                name=tensor_name,
                address=(base_partition, base_addr),
            )
        self.heap.append(mloc)
        self.heap_names.append(tensor_name or "(unnamed)")
        self.total_heap_allocs = self.total_heap_allocs + 1
        self._update_stats()
        self.tree_logger.log(
            f"[HEAP+] {tensor_name or '(unnamed)'}: {bytes_per_partition} B @ {self.heap_curr_addr} {shape} {dtype}",
            len(self.scopes),
        )
        self.logger.debug(
            f"Heap allocation: name={name}, shape={shape}, dtype={dtype}, size={bytes_per_partition} B, "
            f"addr_range=[{self.heap_curr_addr}, {base_addr}), partition={base_partition}, "
            f"heap_depth={len(self.heap)}, free_space={self.heap_curr_addr - self.stack_curr_addr} B"
        )

        return mloc

    def pop_heap(self):
        if not self.heap:
            self.logger.error("Heap underflow: pop called on empty heap")
            kernel_assert(False, "Heap underflow: pop called on empty heap")

        heap_top = self.heap[-1]
        heap_name = self.heap_names[-1]
        N = num_elts(heap_top.shape[1:])
        # Note to FE: the nl.ndarray or the sbuf.ptr should have a way of querying the shape
        bytes_per_partition = N * sizeinbytes(heap_top.dtype)
        self.heap_curr_addr = self.heap_curr_addr + bytes_per_partition
        self.heap_curr_addr = align_to(self.heap_curr_addr - 3, 4)
        self.heap.pop()
        self.heap_names.pop()
        self.tree_logger.log(
            f"[HEAP-] FREE {heap_name}: {bytes_per_partition} B, remaining={len(self.heap)}", len(self.scopes)
        )

    def get_total_space(self):
        return self.upper_bound - self.lower_bound

    def get_free_space(self):
        return self.heap_curr_addr - self.stack_curr_addr

    def get_used_space(self):
        return self.get_total_space() - self.get_free_space()

    def get_stack_curr_addr(self):
        if self.use_auto_alloc:
            self.logger.error("get_stack_curr_addr() is not supported in auto-allocation mode.")
            kernel_assert(False, "get_stack_curr_addr() is not supported in auto-allocation mode")
        return self.stack_curr_addr

    def get_heap_curr_addr(self):
        if self.use_auto_alloc:
            self.logger.error("get_heap_curr_addr() is not supported in auto-allocation mode.")
            kernel_assert(False, "get_heap_curr_addr() is not supported in auto-allocation mode")
        return self.heap_curr_addr

    def align_stack_curr_addr(self, align=32):
        if self.use_auto_alloc:
            self.logger.error("align_stack_curr_addr() is not supported in auto-allocation mode.")
            kernel_assert(False, "align_stack_curr_addr() is not supported in auto-allocation mode")
        self.stack_curr_addr = align_to(self.stack_curr_addr, align)

    def set_name_prefix(self, prefix):
        self.prefix = prefix

    def get_name_prefix(self):
        return self.prefix

    def alloc_hbm(self, shape, dtype, buffer=nl.shared_hbm, name=None):
        """
        Allocate a tensor in HBM with automatic name prefixing.

        :param shape: shape of the tensor to be allocated
        :param dtype: dtype of the tensor to be allocated
        :param buffer: type of the buffer (nl.shared_hbm or nl.hbm)
        :param name: name of the tensor (prefix will be automatically added)
        :return: an HBM tensor
        """
        tensor_name = self._get_prefixed_name(name)
        return nl.ndarray(shape, dtype=dtype, buffer=buffer, name=tensor_name)

    def flush_logs(self):
        """Print buffered allocation logs in tree format."""
        self.tree_logger.flush()


def create_auto_alloc_manager(logger: Optional[Logger] = None):
    """create a default auto allocated SBM initialized with total SBUF space"""
    return SbufManager(0, nl.tile_size.total_available_sbuf_size, logger, use_auto_alloc=True)
