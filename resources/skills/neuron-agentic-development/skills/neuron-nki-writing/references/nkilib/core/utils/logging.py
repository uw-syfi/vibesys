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
Logging system for the NKI environment.

This module provides lightweight logging with environment-based configuration.
Loggers use flat naming and are configured through environment variables or code parameters.

Environment Variables:
  NKILIB_LOG_LEVEL=<level>         Set global default level for all loggers
  NKILIB_LOG_LEVEL_<name>=<level>  Override specific logger (e.g., NKILIB_LOG_LEVEL_SBM=DEBUG)

Priority Order:
  1. NKILIB_LOG_LEVEL_<name> - Env var logger override (highest priority)
  2. NKILIB_LOG_LEVEL        - Env var global override for all loggers
  3. level parameter in code - Development default
  4. _DEFAULT_LOG_LEVEL      - System default (lowest priority)

Usage Examples:

  # get logger with name
  logger = get_logger("SBM")
  logger.info("Allocating buffer") # Shows: [INFO] [SBM] Allocating buffer
  logger.debug("Detailed allocation info") # Does not show - Default level is INFO

  # set log level in code
  logger = get_logger("qkv_tkg", level=LogLevel.DEBUG)
  logger.debug("This shows") # Shows: [DEBUG] [qkv_tkg] This shows

  # override with global env setting
  $ NKILIB_LOG_LEVEL=INFO ...
  logger = get_logger("qkv_tkg", level=LogLevel.DEBUG)
  logger.debug("This shows") # No longer shows - Overridden by global env

  # override with specific env setting
  $ NKILIB_LOG_LEVEL=WARNING LOG_LEVEL_qkv_tkg=DEBUG ...
  logger = get_logger("qkv_tkg")
  logger.debug("This shows") # Shows: [DEBUG] [qkv_tkg] This shows

  # Quick logging with default logger instance
  from nkilib.core.utils.logging import logger
  logger.info("message")

"""

import os
import sys
from enum import Enum
from typing import Optional

import nki.language as nl


class LogLevel(Enum):
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3
    OFF = 999

    @staticmethod
    def from_string(level: str) -> "LogLevel":
        return _STRING_TO_LOG_LEVEL[level]


_STRING_TO_LOG_LEVEL = {
    "DEBUG": LogLevel.DEBUG,
    "INFO": LogLevel.INFO,
    "WARN": LogLevel.WARN,
    "ERROR": LogLevel.ERROR,
    "OFF": LogLevel.OFF,
}

_DEFAULT_LOG_LEVEL = LogLevel.INFO


# Do not use; prefer get_logger() from below
class Logger(nl.NKIObject):
    def __init__(self, name: str, level: LogLevel = _DEFAULT_LOG_LEVEL):
        self.name = name
        self.level = level

    def _should_log(self, level: LogLevel) -> bool:
        return level.value >= self.level.value

    def is_enabled_for(self, level: LogLevel) -> bool:
        """Check if level is enabled before computing expensive message"""
        return self._should_log(level)

    def _print(self, msg: str, prefix: str = ""):
        name_prefix = f"[{self.name}] " if self.name else ""
        print(f"{prefix}{name_prefix}{msg}")

    def debug(self, msg: str):
        if self._should_log(LogLevel.DEBUG):
            self._print(msg, prefix="[DEBUG] ")

    def info(self, msg: str):
        if self._should_log(LogLevel.INFO):
            self._print(msg, prefix="[INFO] ")

    def warn(self, msg: str):
        if self._should_log(LogLevel.WARN):
            self._print(msg, prefix="[WARN] ")

    def error(self, msg: str):
        if self._should_log(LogLevel.ERROR):
            self._print(msg, prefix="[ERROR] ")


# Env var prefix logging looks for
_ENV_VAR_NAME = "NKILIB_LOG_LEVEL"
_ENV_VAR_PREFIX = _ENV_VAR_NAME + "_"


def _init_from_env_py() -> tuple[Optional[LogLevel], dict[str, LogLevel]]:
    """Initialize global and specific logger levels from environment variables.
    Checking environment variables here works because this is not part of kernel code.
    """
    specific_levels = {}
    global_level = None

    for key, value in os.environ.items():
        # Global env config
        if key == _ENV_VAR_NAME:
            try:
                global_level = LogLevel.from_string(value.upper())
            except KeyError:
                print(f"Warning: Invalid {_ENV_VAR_NAME}='{value}'", file=sys.stderr)

        # Per-logger env config
        elif key.startswith(_ENV_VAR_PREFIX):
            logger_name = key.removeprefix(_ENV_VAR_PREFIX)
            try:
                specific_levels[logger_name] = LogLevel.from_string(value.upper())
            except KeyError:
                print(f"Warning: Invalid log level '{value}' for {logger_name}", file=sys.stderr)

    return global_level, specific_levels


_ENV_GLOBAL_LOG_LEVEL, _ENV_LOG_LEVELS = _init_from_env_py()


def get_logger(name: str, level: LogLevel = _DEFAULT_LOG_LEVEL) -> Logger:
    """
    Get a logger with the specified name and level.

    The logger's effective level is determined by the priority order detailed below.

    Priority Order:
      1. NKILIB_LOG_LEVEL_<name> - Env var logger override (highest priority)
      2. NKILIB_LOG_LEVEL        - Env var global override for all loggers
      3. level parameter in code - Development default
      4. _DEFAULT_LOG_LEVEL      - System default (lowest priority)

    Args:
        name: Logger name for identification in output and environment matching.
        level: Code-level default if no environment overrides found.
               Defaults to the system default level if not set.

    Returns:
        Logger instance configured according to priority order.

    """

    env_log_level = _ENV_LOG_LEVELS.get(name)
    if env_log_level != None:
        return Logger(name, env_log_level)

    if _ENV_GLOBAL_LOG_LEVEL != None:
        return Logger(name, _ENV_GLOBAL_LOG_LEVEL)

    return Logger(name, level)


# global logger instance
# backward compatibility to prevent breaking existing kernel code
logger = get_logger("")
