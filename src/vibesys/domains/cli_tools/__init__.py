"""Native command-line tools domain definition.

Hosts optimization of locally built native command-line programs, judged by
observable behavior and a task-owned measurement.
"""

from __future__ import annotations

from vibesys.constants import DomainName
from vibesys.domains.base import DomainDefinition, prompt_dir_beside

DEFINITION = DomainDefinition(
    name=DomainName.CLI_TOOLS,
    prompt_dir=prompt_dir_beside(__file__),
)
