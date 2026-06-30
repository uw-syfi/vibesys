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
Tree-style logger for hierarchical allocation logging.
"""

from dataclasses import dataclass

import nki.language as nl

from .logging import Logger, LogLevel


@dataclass
class LogEntry(nl.NKIObject):
    """Buffered log entry for tree-style printing."""

    msg: str
    depth: int
    is_stack: bool  # True for stack, False for heap
    is_scope_boundary: bool = False  # True for open/close scope


class TreeLogger(nl.NKIObject):
    """Buffers logs and prints in tree format."""

    def __init__(self, name: str, logger: Logger):
        self.name = name
        self.logger = logger
        self.stack_logs = []
        self._enabled = logger.is_enabled_for(LogLevel.DEBUG)

    def log(self, msg: str, depth: int, is_scope_boundary: bool = False):
        if not self._enabled:
            return
        self.stack_logs.append(LogEntry(msg, depth, True, is_scope_boundary))

    def _has_depth_after(self, logs, start_idx: int, target_depth: int) -> bool:
        for j in range(start_idx, len(logs)):
            if logs[j].depth == target_depth:
                return True
        return False

    def _tree_prefix(self, depth: int, is_last: bool, parent_continues) -> str:
        if depth == 0:
            return ""
        prefix = ""
        for i in range(depth - 1):
            if i < len(parent_continues) and parent_continues[i]:
                prefix += "│   "
            else:
                prefix += "    "
        if is_last:
            prefix += "└── "
        else:
            prefix += "├── "
        return prefix

    def flush(self):
        """Print buffered logs in tree format."""
        if not self.stack_logs:
            return

        self.logger.debug(f"[{self.name}] Allocations:")
        self._print_tree(self.stack_logs)
        self.stack_logs.clear()

    def _print_tree(self, logs):
        # First pass: mark which entries are last at their depth
        is_last = []
        for i in range(len(logs)):
            is_last.append(True)

        for i in range(len(logs)):
            depth_i = logs[i].depth
            for j in range(i + 1, len(logs)):
                depth_j = logs[j].depth
                if depth_j < depth_i:
                    break
                if depth_j == depth_i:
                    is_last[i] = False
                    break

        # Second pass: print with prefixes
        for i in range(len(logs)):
            entry = logs[i]
            prefix = ""
            if entry.depth > 0:
                for d in range(entry.depth - 1):
                    prefix = prefix + "│   "
                if is_last[i]:
                    prefix = prefix + "└── "
                else:
                    prefix = prefix + "├── "
            self.logger.debug(f"    {prefix}{entry.msg}")
