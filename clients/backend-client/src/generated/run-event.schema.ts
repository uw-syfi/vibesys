/* Generated from the Python protocol models. Do not edit. */

const runEventSchema: Record<string, unknown> = {
  "$defs": {
    "AgentExecutionActivityData": {
      "description": "Complete current activity for an active agent execution.",
      "properties": {
        "kind": {
          "const": "agent_execution_activity_changed",
          "default": "agent_execution_activity_changed",
          "title": "Kind",
          "type": "string"
        },
        "mode": {
          "enum": [
            "thinking",
            "responding",
            "tool",
            "waiting"
          ],
          "title": "Mode",
          "type": "string"
        },
        "summary": {
          "title": "Summary",
          "type": "string"
        },
        "tool": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Tool"
        }
      },
      "required": [
        "kind",
        "mode",
        "summary"
      ],
      "title": "AgentExecutionActivityData",
      "type": "object"
    },
    "AgentExecutionFinishedData": {
      "description": "Terminal result for one agent execution.",
      "properties": {
        "kind": {
          "const": "agent_execution_finished",
          "default": "agent_execution_finished",
          "title": "Kind",
          "type": "string"
        },
        "result": {
          "default": null,
          "title": "Result"
        },
        "error": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Error"
        }
      },
      "title": "AgentExecutionFinishedData",
      "type": "object",
      "required": [
        "kind"
      ]
    },
    "AgentExecutionStartedData": {
      "description": "Semantic context for one prompt-to-result agent execution.",
      "properties": {
        "kind": {
          "const": "agent_execution_started",
          "default": "agent_execution_started",
          "title": "Kind",
          "type": "string"
        },
        "stage": {
          "title": "Stage",
          "type": "string"
        },
        "attempt": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Attempt"
        },
        "system_prompt": {
          "default": "",
          "title": "System Prompt",
          "type": "string"
        },
        "user_prompt": {
          "default": "",
          "title": "User Prompt",
          "type": "string"
        },
        "activity": {
          "$ref": "#/$defs/AgentExecutionActivityData"
        },
        "provider": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Provider"
        },
        "model": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Model"
        }
      },
      "required": [
        "kind",
        "stage",
        "activity"
      ],
      "title": "AgentExecutionStartedData",
      "type": "object"
    },
    "AgentOutputChunkData": {
      "description": "Incremental output produced during agent execution.",
      "properties": {
        "kind": {
          "const": "agent_output_chunk",
          "default": "agent_output_chunk",
          "title": "Kind",
          "type": "string"
        },
        "channel": {
          "enum": [
            "assistant",
            "analysis",
            "tool",
            "diagnostic",
            "prompt"
          ],
          "title": "Channel",
          "type": "string"
        },
        "content": {
          "title": "Content",
          "type": "string"
        },
        "status": {
          "anyOf": [
            {
              "$ref": "#/$defs/AgentStatusData"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        }
      },
      "required": [
        "kind",
        "channel",
        "content"
      ],
      "title": "AgentOutputChunkData",
      "type": "object"
    },
    "AgentStatusData": {
      "description": "Structured progress readings for one agent invocation.\n\nCarried on presentation events so renderers can format their own status\nprefix (e.g. ``[Round 3/24 | Implementer | 12.3s | 20k/1.0M]``) without\nthe server baking any layout or styling into the payload.",
      "properties": {
        "progress": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Progress"
        },
        "agent_label": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Agent Label"
        },
        "elapsed_seconds": {
          "default": 0.0,
          "title": "Elapsed Seconds",
          "type": "number"
        },
        "input_tokens": {
          "default": 0,
          "title": "Input Tokens",
          "type": "integer"
        },
        "context_window": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Context Window"
        }
      },
      "title": "AgentStatusData",
      "type": "object"
    },
    "BenchmarkResultData": {
      "description": "Metric result emitted by a benchmark stage.",
      "properties": {
        "kind": {
          "const": "benchmark_result",
          "default": "benchmark_result",
          "title": "Kind",
          "type": "string"
        },
        "metric": {
          "title": "Metric",
          "type": "string"
        },
        "value": {
          "title": "Value",
          "type": "number"
        },
        "unit": {
          "title": "Unit",
          "type": "string"
        }
      },
      "required": [
        "kind",
        "metric",
        "value",
        "unit"
      ],
      "title": "BenchmarkResultData",
      "type": "object"
    },
    "ChatData": {
      "description": "Completed answer and optional thread-turn identity.",
      "properties": {
        "kind": {
          "const": "chat",
          "default": "chat",
          "title": "Kind",
          "type": "string"
        },
        "answer": {
          "title": "Answer",
          "type": "string"
        },
        "thread_title": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Thread Title"
        },
        "invocation_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Invocation Id"
        }
      },
      "required": [
        "kind",
        "answer"
      ],
      "title": "ChatData",
      "type": "object"
    },
    "ChatThreadCreatedData": {
      "description": "Identity and resolved agent settings for one experiment-chat thread.\n\nReplayed by clients to rebuild the thread list; the default thread is\nimplicit and never records one of these.",
      "properties": {
        "kind": {
          "const": "chat_thread_created",
          "default": "chat_thread_created",
          "title": "Kind",
          "type": "string"
        },
        "thread_id": {
          "title": "Thread Id",
          "type": "string"
        },
        "title": {
          "default": "",
          "title": "Title",
          "type": "string"
        },
        "provider": {
          "title": "Provider",
          "type": "string"
        },
        "model": {
          "title": "Model",
          "type": "string"
        },
        "created_at": {
          "format": "date-time",
          "title": "Created At",
          "type": "string"
        }
      },
      "required": [
        "kind",
        "thread_id",
        "provider",
        "model",
        "created_at"
      ],
      "title": "ChatThreadCreatedData",
      "type": "object"
    },
    "CommandResultPayload": {
      "description": "Structured result of a command-style tool execution.",
      "properties": {
        "kind": {
          "const": "command",
          "default": "command",
          "title": "Kind",
          "type": "string"
        },
        "stdout": {
          "title": "Stdout",
          "type": "string"
        },
        "stderr": {
          "title": "Stderr",
          "type": "string"
        },
        "exit_code": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Exit Code"
        },
        "duration": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Duration"
        }
      },
      "required": [
        "kind",
        "stdout",
        "stderr"
      ],
      "title": "CommandResultPayload",
      "type": "object"
    },
    "ConfigurationFailedData": {
      "description": "Diagnostic details for configuration-stage failure.",
      "properties": {
        "kind": {
          "const": "configuration_failed",
          "default": "configuration_failed",
          "title": "Kind",
          "type": "string"
        },
        "code": {
          "title": "Code",
          "type": "string"
        },
        "stage": {
          "title": "Stage",
          "type": "string"
        },
        "message": {
          "title": "Message",
          "type": "string"
        },
        "usage": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Usage"
        },
        "exit_code": {
          "title": "Exit Code",
          "type": "integer"
        }
      },
      "required": [
        "kind",
        "code",
        "stage",
        "message",
        "exit_code"
      ],
      "title": "ConfigurationFailedData",
      "type": "object"
    },
    "Diagnostic": {
      "additionalProperties": false,
      "description": "Structured, provider-neutral description of an operator diagnostic.\n\nFrozen for the same reason as ``RunEvent``: diagnostics ride along on\nreplayed events, which readers share rather than copy.",
      "properties": {
        "id": {
          "title": "Id",
          "type": "string"
        },
        "code": {
          "title": "Code",
          "type": "string"
        },
        "summary": {
          "title": "Summary",
          "type": "string"
        },
        "detail": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Detail"
        },
        "hint": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Hint"
        },
        "scope": {
          "$ref": "#/$defs/DiagnosticScope"
        },
        "severity": {
          "$ref": "#/$defs/DiagnosticSeverity",
          "default": "error"
        },
        "retryability": {
          "$ref": "#/$defs/DiagnosticRetryability",
          "default": "unknown"
        },
        "cause_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Cause Id"
        },
        "debug_ref": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Debug Ref"
        },
        "source": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Source"
        },
        "validation_paths": {
          "anyOf": [
            {
              "items": {
                "items": {
                  "anyOf": [
                    {
                      "type": "string"
                    },
                    {
                      "type": "integer"
                    }
                  ]
                },
                "type": "array"
              },
              "type": "array"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Validation Paths"
        }
      },
      "required": [
        "code",
        "summary",
        "scope"
      ],
      "title": "Diagnostic",
      "type": "object"
    },
    "DiagnosticRetryability": {
      "description": "Whether retrying the failed operation is expected to help.",
      "enum": [
        "automatic",
        "manual",
        "never",
        "unknown"
      ],
      "title": "DiagnosticRetryability",
      "type": "string"
    },
    "DiagnosticScope": {
      "description": "Boundary at which a diagnostic was raised.",
      "enum": [
        "configuration",
        "invocation",
        "phase",
        "run",
        "request",
        "protocol",
        "transport"
      ],
      "title": "DiagnosticScope",
      "type": "string"
    },
    "DiagnosticSeverity": {
      "description": "Operator-visible seriousness of a diagnostic.",
      "enum": [
        "warning",
        "error",
        "fatal"
      ],
      "title": "DiagnosticSeverity",
      "type": "string"
    },
    "EventStatus": {
      "description": "Lifecycle and command states reported in events.",
      "enum": [
        "active",
        "answered",
        "pending",
        "consumed",
        "completed",
        "failed",
        "cancelled",
        "interrupted"
      ],
      "title": "EventStatus",
      "type": "string"
    },
    "EventType": {
      "description": "Wire event kinds persisted in the event log.",
      "enum": [
        "server_started",
        "server_ready",
        "configuration_failed",
        "run_started",
        "experiments_changed",
        "run_interrupted",
        "run_status_changed",
        "chat",
        "chat_thread_created",
        "status_query",
        "control",
        "invocation_started",
        "invocation_finished",
        "agent_execution_started",
        "agent_execution_activity_changed",
        "agent_execution_finished",
        "phase_started",
        "phase_finished",
        "agent_output_chunk",
        "subprocess_output",
        "judge_result",
        "benchmark_result",
        "round_finished",
        "run_finished",
        "run_failed",
        "output",
        "tool_call",
        "tool_result",
        "todo_update",
        "usage_update",
        "gate_started",
        "gate_finished",
        "workspace_snapshot",
        "run_configured",
        "framework_warning"
      ],
      "title": "EventType",
      "type": "string"
    },
    "ExperimentsChangedData": {
      "description": "Reason and revision for a changed experiment projection.",
      "properties": {
        "kind": {
          "const": "experiments_changed",
          "default": "experiments_changed",
          "title": "Kind",
          "type": "string"
        },
        "reason": {
          "enum": [
            "project_attached",
            "active_hypothesis_changed",
            "round_persisted"
          ],
          "title": "Reason",
          "type": "string"
        },
        "revision": {
          "anyOf": [
            {
              "minimum": 0,
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Revision"
        }
      },
      "required": [
        "kind",
        "reason"
      ],
      "title": "ExperimentsChangedData",
      "type": "object"
    },
    "FrameworkSource": {
      "description": "Closed set of framework subsystems that emit framework events.",
      "enum": [
        "gates",
        "git_tracking",
        "loop",
        "gpu",
        "skypilot",
        "other"
      ],
      "title": "FrameworkSource",
      "type": "string"
    },
    "FrameworkWarningData": {
      "description": "A non-fatal framework fault an operator should see.\n\nThe server projection also lifts this payload into the wire event's\n``diagnostic`` field so diagnostic-oriented clients need no new handling.",
      "properties": {
        "kind": {
          "const": "framework_warning",
          "default": "framework_warning",
          "title": "Kind",
          "type": "string"
        },
        "summary": {
          "title": "Summary",
          "type": "string"
        },
        "detail": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Detail"
        },
        "source": {
          "$ref": "#/$defs/FrameworkSource",
          "default": "other"
        },
        "source_label": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Source Label"
        }
      },
      "required": [
        "kind",
        "summary"
      ],
      "title": "FrameworkWarningData",
      "type": "object"
    },
    "GateFinishedData": {
      "description": "Outcome of one framework gate; envelope status carries pass or fail.\n\n``metric``/``value``/``unit`` are set only on a passing benchmark gate.\n``unit`` keeps the historical fallback of the metric name when the\ncontract declares no unit. ``output_tail`` carries the trailing command\noutput on failure.",
      "properties": {
        "kind": {
          "const": "gate_finished",
          "default": "gate_finished",
          "title": "Kind",
          "type": "string"
        },
        "gate": {
          "$ref": "#/$defs/GateKind"
        },
        "recipe": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Recipe"
        },
        "reused": {
          "default": false,
          "title": "Reused",
          "type": "boolean"
        },
        "metric": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Metric"
        },
        "value": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Value"
        },
        "unit": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Unit"
        },
        "output_tail": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Output Tail"
        },
        "source": {
          "$ref": "#/$defs/FrameworkSource",
          "default": "gates"
        },
        "source_label": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Source Label"
        }
      },
      "required": [
        "kind",
        "gate"
      ],
      "title": "GateFinishedData",
      "type": "object"
    },
    "GateKind": {
      "description": "Closed set of framework-owned gates a candidate passes through.",
      "enum": [
        "validation",
        "accuracy",
        "benchmark"
      ],
      "title": "GateKind",
      "type": "string"
    },
    "GateStartedData": {
      "description": "One framework gate began evaluating the current candidate.",
      "properties": {
        "kind": {
          "const": "gate_started",
          "default": "gate_started",
          "title": "Kind",
          "type": "string"
        },
        "gate": {
          "$ref": "#/$defs/GateKind"
        },
        "recipe": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Recipe"
        },
        "command": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Command"
        },
        "source": {
          "$ref": "#/$defs/FrameworkSource",
          "default": "gates"
        },
        "source_label": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Source Label"
        }
      },
      "required": [
        "kind",
        "gate"
      ],
      "title": "GateStartedData",
      "type": "object"
    },
    "InvocationFinishedData": {
      "description": "Result or error recorded when a model invocation ends.",
      "properties": {
        "kind": {
          "const": "invocation_finished",
          "default": "invocation_finished",
          "title": "Kind",
          "type": "string"
        },
        "result": {
          "default": null,
          "title": "Result"
        },
        "error": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Error"
        }
      },
      "title": "InvocationFinishedData",
      "type": "object",
      "required": [
        "kind"
      ]
    },
    "InvocationStartedData": {
      "description": "Prompts submitted at the start of a model invocation.",
      "properties": {
        "kind": {
          "const": "invocation_started",
          "default": "invocation_started",
          "title": "Kind",
          "type": "string"
        },
        "system_prompt": {
          "title": "System Prompt",
          "type": "string"
        },
        "user_prompt": {
          "title": "User Prompt",
          "type": "string"
        }
      },
      "required": [
        "kind",
        "system_prompt",
        "user_prompt"
      ],
      "title": "InvocationStartedData",
      "type": "object"
    },
    "JsonResultPayload": {
      "description": "A tool result that is a JSON object or array, already parsed.",
      "properties": {
        "kind": {
          "const": "json",
          "default": "json",
          "title": "Kind",
          "type": "string"
        },
        "value": {
          "anyOf": [
            {
              "additionalProperties": true,
              "type": "object"
            },
            {
              "items": {},
              "type": "array"
            }
          ],
          "title": "Value"
        }
      },
      "required": [
        "kind",
        "value"
      ],
      "title": "JsonResultPayload",
      "type": "object"
    },
    "JudgeResultData": {
      "description": "Verdict and feedback returned by the judge.",
      "properties": {
        "kind": {
          "const": "judge_result",
          "default": "judge_result",
          "title": "Kind",
          "type": "string"
        },
        "verdict": {
          "enum": [
            "pass",
            "fail"
          ],
          "title": "Verdict",
          "type": "string"
        },
        "feedback": {
          "title": "Feedback",
          "type": "string"
        },
        "attempt": {
          "title": "Attempt",
          "type": "integer"
        }
      },
      "required": [
        "kind",
        "verdict",
        "feedback",
        "attempt"
      ],
      "title": "JudgeResultData",
      "type": "object"
    },
    "OutputData": {
      "description": "Captured line of server output and its stream.",
      "properties": {
        "kind": {
          "const": "output",
          "default": "output",
          "title": "Kind",
          "type": "string"
        },
        "stream": {
          "enum": [
            "stdout",
            "stderr"
          ],
          "title": "Stream",
          "type": "string"
        },
        "source": {
          "default": "backend",
          "title": "Source",
          "type": "string"
        },
        "content": {
          "title": "Content",
          "type": "string"
        }
      },
      "required": [
        "kind",
        "stream",
        "content"
      ],
      "title": "OutputData",
      "type": "object"
    },
    "PhaseData": {
      "description": "Name and optional attempt number for a loop phase.",
      "properties": {
        "kind": {
          "const": "phase",
          "default": "phase",
          "title": "Kind",
          "type": "string"
        },
        "phase": {
          "title": "Phase",
          "type": "string"
        },
        "attempt": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Attempt"
        }
      },
      "required": [
        "kind",
        "phase"
      ],
      "title": "PhaseData",
      "type": "object"
    },
    "RoundFinishedData": {
      "description": "Summary of attempt, judge, and performance outcomes for a round.",
      "properties": {
        "kind": {
          "const": "round_finished",
          "default": "round_finished",
          "title": "Kind",
          "type": "string"
        },
        "attempts": {
          "title": "Attempts",
          "type": "integer"
        },
        "judge_verdict": {
          "enum": [
            "pass",
            "fail",
            "skipped"
          ],
          "title": "Judge Verdict",
          "type": "string"
        },
        "perf_metric": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Perf Metric"
        },
        "perf_unit": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Perf Unit"
        },
        "profile_skipped": {
          "title": "Profile Skipped",
          "type": "boolean"
        }
      },
      "required": [
        "kind",
        "attempts",
        "judge_verdict",
        "profile_skipped"
      ],
      "title": "RoundFinishedData",
      "type": "object"
    },
    "RunConfiguredData": {
      "description": "One per run: the resolved configuration a loop starts with.",
      "properties": {
        "kind": {
          "const": "run_configured",
          "default": "run_configured",
          "title": "Kind",
          "type": "string"
        },
        "run_log_path": {
          "title": "Run Log Path",
          "type": "string"
        },
        "project_root": {
          "title": "Project Root",
          "type": "string"
        },
        "model": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Model"
        },
        "objective": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Objective"
        },
        "search_policy": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Search Policy"
        },
        "benchmark_contract": {
          "default": false,
          "title": "Benchmark Contract",
          "type": "boolean"
        },
        "pareto_objectives": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Pareto Objectives"
        },
        "source": {
          "$ref": "#/$defs/FrameworkSource",
          "default": "loop"
        }
      },
      "required": [
        "kind",
        "run_log_path",
        "project_root"
      ],
      "title": "RunConfiguredData",
      "type": "object"
    },
    "RunFailedData": {
      "description": "Why a run ended without a result to keep, with the counts behind it.",
      "properties": {
        "kind": {
          "const": "run_failed",
          "default": "run_failed",
          "title": "Kind",
          "type": "string"
        },
        "failure": {
          "$ref": "#/$defs/RunFailure"
        }
      },
      "required": [
        "kind",
        "failure"
      ],
      "title": "RunFailedData",
      "type": "object"
    },
    "RunFailure": {
      "additionalProperties": false,
      "description": "What a failed run did and why it stopped, as data; frontends choose the wording.\n\n``reason`` is the strategy's own account of the stop. ``workstreams_started`` counts\nattempts core admitted, against the ``workstream_budget`` the run was allowed.\n``candidates_kept`` counts settled candidates eligible for adoption.",
      "properties": {
        "kind": {
          "$ref": "#/$defs/RunFailureKind"
        },
        "reason": {
          "title": "Reason",
          "type": "string"
        },
        "workstreams_started": {
          "minimum": 0,
          "title": "Workstreams Started",
          "type": "integer"
        },
        "workstream_budget": {
          "minimum": 0,
          "title": "Workstream Budget",
          "type": "integer"
        },
        "candidates_kept": {
          "minimum": 0,
          "title": "Candidates Kept",
          "type": "integer"
        }
      },
      "required": [
        "kind",
        "reason",
        "workstreams_started",
        "workstream_budget",
        "candidates_kept"
      ],
      "title": "RunFailure",
      "type": "object"
    },
    "RunFailureKind": {
      "description": "Why a run ended without a result the operator can keep.",
      "enum": [
        "budget_exhausted",
        "deadline",
        "no_result"
      ],
      "title": "RunFailureKind",
      "type": "string"
    },
    "RunInterruptedData": {
      "description": "Reason and optional signal for an interrupted run.",
      "properties": {
        "kind": {
          "const": "run_interrupted",
          "default": "run_interrupted",
          "title": "Kind",
          "type": "string"
        },
        "reason": {
          "title": "Reason",
          "type": "string"
        },
        "signal": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Signal"
        }
      },
      "required": [
        "kind",
        "reason"
      ],
      "title": "RunInterruptedData",
      "type": "object"
    },
    "RunStartedData": {
      "description": "Initial input and loop settings for a run.",
      "properties": {
        "kind": {
          "const": "run_started",
          "default": "run_started",
          "title": "Kind",
          "type": "string"
        },
        "outer_loop": {
          "title": "Outer Loop",
          "type": "string"
        },
        "input": {
          "title": "Input",
          "type": "string"
        },
        "max_rounds": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Max Rounds"
        },
        "expected_roles": {
          "default": [],
          "items": {
            "type": "string"
          },
          "title": "Expected Roles",
          "type": "array"
        }
      },
      "required": [
        "kind",
        "outer_loop",
        "input"
      ],
      "title": "RunStartedData",
      "type": "object"
    },
    "RunStatus": {
      "description": "Lifecycle status of one run, as frontends observe it.\n\nThis is the authoritative closed set for the ``status`` field of\n``RunSnapshot`` and of ``RunStatusChangedData``; the generated TypeScript\nprotocol types derive their union from it.\n\n``PAUSING`` and ``PAUSED`` are distinct because a pause is only applied at\nan invocation boundary: ``/pause`` records the request, and the run keeps\nexecuting the call already in flight until it reaches that boundary.\n``STOPPING`` and ``STOPPED`` split the same way for ``/stop``, whose\nboundary is where the run ends instead of where it parks.",
      "enum": [
        "starting",
        "running",
        "pausing",
        "paused",
        "stopping",
        "stopped",
        "completed",
        "failed"
      ],
      "title": "RunStatus",
      "type": "string"
    },
    "RunStatusChangedData": {
      "description": "One move of the run through its lifecycle.\n\nCarries the whole transition so a client folds the status instead of\ninferring it: ``status`` is the new value and ``previous`` the one it\nreplaced. Which invocation boundary a pause landed on is on the event\nenvelope (``agent_kind``, ``round_label``, ``execution_id``) like every\nother execution-scoped fact, not repeated here.",
      "properties": {
        "kind": {
          "const": "run_status_changed",
          "default": "run_status_changed",
          "title": "Kind",
          "type": "string"
        },
        "status": {
          "$ref": "#/$defs/RunStatus"
        },
        "previous": {
          "$ref": "#/$defs/RunStatus"
        }
      },
      "required": [
        "kind",
        "status",
        "previous"
      ],
      "title": "RunStatusChangedData",
      "type": "object"
    },
    "ServerReadyData": {
      "description": "Transport details emitted once the server is ready.",
      "properties": {
        "kind": {
          "const": "server_ready",
          "default": "server_ready",
          "title": "Kind",
          "type": "string"
        },
        "socket_protocol": {
          "const": "jsonl",
          "default": "jsonl",
          "title": "Socket Protocol",
          "type": "string"
        }
      },
      "title": "ServerReadyData",
      "type": "object",
      "required": [
        "kind"
      ]
    },
    "SubprocessOutputData": {
      "description": "Captured output from a managed subprocess.",
      "properties": {
        "kind": {
          "const": "subprocess_output",
          "default": "subprocess_output",
          "title": "Kind",
          "type": "string"
        },
        "process_id": {
          "title": "Process Id",
          "type": "string"
        },
        "process_kind": {
          "title": "Process Kind",
          "type": "string"
        },
        "stream": {
          "enum": [
            "stdout",
            "stderr"
          ],
          "title": "Stream",
          "type": "string"
        },
        "content": {
          "title": "Content",
          "type": "string"
        }
      },
      "required": [
        "kind",
        "process_id",
        "process_kind",
        "stream",
        "content"
      ],
      "title": "SubprocessOutputData",
      "type": "object"
    },
    "TodoItemData": {
      "description": "One item in the agent's reported todo list.",
      "properties": {
        "content": {
          "title": "Content",
          "type": "string"
        },
        "status": {
          "title": "Status",
          "type": "string"
        }
      },
      "required": [
        "content",
        "status"
      ],
      "title": "TodoItemData",
      "type": "object"
    },
    "TodoUpdateData": {
      "description": "Current todo list reported by an agent.",
      "properties": {
        "kind": {
          "const": "todo_update",
          "default": "todo_update",
          "title": "Kind",
          "type": "string"
        },
        "todos": {
          "items": {
            "$ref": "#/$defs/TodoItemData"
          },
          "title": "Todos",
          "type": "array"
        }
      },
      "title": "TodoUpdateData",
      "type": "object",
      "required": [
        "kind"
      ]
    },
    "ToolCallData": {
      "description": "Tool name, call identity, and arguments emitted by an agent.",
      "properties": {
        "kind": {
          "const": "tool_call",
          "default": "tool_call",
          "title": "Kind",
          "type": "string"
        },
        "tool": {
          "title": "Tool",
          "type": "string"
        },
        "call_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Call Id"
        },
        "args": {
          "additionalProperties": true,
          "title": "Args",
          "type": "object"
        },
        "status": {
          "anyOf": [
            {
              "$ref": "#/$defs/AgentStatusData"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        }
      },
      "required": [
        "kind",
        "tool"
      ],
      "title": "ToolCallData",
      "type": "object"
    },
    "ToolResultData": {
      "description": "Raw tool result and optional structured rendering payload.",
      "properties": {
        "kind": {
          "const": "tool_result",
          "default": "tool_result",
          "title": "Kind",
          "type": "string"
        },
        "tool": {
          "title": "Tool",
          "type": "string"
        },
        "call_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Call Id"
        },
        "content": {
          "title": "Content",
          "type": "string"
        },
        "is_error": {
          "default": false,
          "title": "Is Error",
          "type": "boolean"
        },
        "payload": {
          "anyOf": [
            {
              "discriminator": {
                "mapping": {
                  "command": "#/$defs/CommandResultPayload",
                  "json": "#/$defs/JsonResultPayload"
                },
                "propertyName": "kind"
              },
              "oneOf": [
                {
                  "$ref": "#/$defs/CommandResultPayload"
                },
                {
                  "$ref": "#/$defs/JsonResultPayload"
                }
              ]
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Payload"
        }
      },
      "required": [
        "kind",
        "tool",
        "content"
      ],
      "title": "ToolResultData",
      "type": "object"
    },
    "UsageUpdateData": {
      "description": "Token usage reported by the active model.",
      "properties": {
        "kind": {
          "const": "usage_update",
          "default": "usage_update",
          "title": "Kind",
          "type": "string"
        },
        "input_tokens": {
          "title": "Input Tokens",
          "type": "integer"
        },
        "context_window": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Context Window"
        },
        "model": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Model"
        }
      },
      "required": [
        "kind",
        "input_tokens"
      ],
      "title": "UsageUpdateData",
      "type": "object"
    },
    "WorkspaceSnapshotData": {
      "description": "A Git tracker outcome: a snapshot, baseline, or exclusion change.\n\nExactly one aspect is populated per event: a snapshot attempt carries\n``label`` (``commit`` is None when there was nothing to commit), a\ntrusted-input baseline carries ``baseline``, and a snapshot-exclusion\nchange carries ``excluded_paths``.",
      "properties": {
        "kind": {
          "const": "workspace_snapshot",
          "default": "workspace_snapshot",
          "title": "Kind",
          "type": "string"
        },
        "label": {
          "default": "",
          "title": "Label",
          "type": "string"
        },
        "commit": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Commit"
        },
        "baseline": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Baseline"
        },
        "excluded_paths": {
          "default": [],
          "items": {
            "type": "string"
          },
          "title": "Excluded Paths",
          "type": "array"
        },
        "source": {
          "$ref": "#/$defs/FrameworkSource",
          "default": "git_tracking"
        }
      },
      "title": "WorkspaceSnapshotData",
      "type": "object",
      "required": [
        "kind"
      ]
    }
  },
  "additionalProperties": false,
  "description": "One reproducible human, control, or invocation event.\n\nFrozen: a recorded event is a durable fact. Readers that need a variant\nbuild one with ``model_copy(update=...)`` rather than mutating a shared\nobject, which lets ``EventStore`` replay history without copying it.",
  "properties": {
    "protocol_version": {
      "const": 1,
      "default": 1,
      "title": "Protocol Version",
      "type": "integer"
    },
    "sequence": {
      "default": 0,
      "minimum": 0,
      "title": "Sequence",
      "type": "integer"
    },
    "run_id": {
      "default": "",
      "title": "Run Id",
      "type": "string"
    },
    "timestamp": {
      "format": "date-time",
      "title": "Timestamp",
      "type": "string"
    },
    "type": {
      "$ref": "#/$defs/EventType"
    },
    "text": {
      "default": "",
      "title": "Text",
      "type": "string"
    },
    "diagnostic": {
      "anyOf": [
        {
          "$ref": "#/$defs/Diagnostic"
        },
        {
          "type": "null"
        }
      ],
      "default": null
    },
    "status": {
      "anyOf": [
        {
          "$ref": "#/$defs/EventStatus"
        },
        {
          "type": "null"
        }
      ],
      "default": null
    },
    "round_label": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Round Label"
    },
    "agent_kind": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Agent Kind"
    },
    "invocation_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Invocation Id"
    },
    "execution_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Execution Id"
    },
    "chat_thread_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Chat Thread Id"
    },
    "data": {
      "anyOf": [
        {
          "discriminator": {
            "mapping": {
              "agent_execution_activity_changed": "#/$defs/AgentExecutionActivityData",
              "agent_execution_finished": "#/$defs/AgentExecutionFinishedData",
              "agent_execution_started": "#/$defs/AgentExecutionStartedData",
              "agent_output_chunk": "#/$defs/AgentOutputChunkData",
              "benchmark_result": "#/$defs/BenchmarkResultData",
              "chat": "#/$defs/ChatData",
              "chat_thread_created": "#/$defs/ChatThreadCreatedData",
              "configuration_failed": "#/$defs/ConfigurationFailedData",
              "experiments_changed": "#/$defs/ExperimentsChangedData",
              "framework_warning": "#/$defs/FrameworkWarningData",
              "gate_finished": "#/$defs/GateFinishedData",
              "gate_started": "#/$defs/GateStartedData",
              "invocation_finished": "#/$defs/InvocationFinishedData",
              "invocation_started": "#/$defs/InvocationStartedData",
              "judge_result": "#/$defs/JudgeResultData",
              "output": "#/$defs/OutputData",
              "phase": "#/$defs/PhaseData",
              "round_finished": "#/$defs/RoundFinishedData",
              "run_configured": "#/$defs/RunConfiguredData",
              "run_failed": "#/$defs/RunFailedData",
              "run_interrupted": "#/$defs/RunInterruptedData",
              "run_started": "#/$defs/RunStartedData",
              "run_status_changed": "#/$defs/RunStatusChangedData",
              "server_ready": "#/$defs/ServerReadyData",
              "subprocess_output": "#/$defs/SubprocessOutputData",
              "todo_update": "#/$defs/TodoUpdateData",
              "tool_call": "#/$defs/ToolCallData",
              "tool_result": "#/$defs/ToolResultData",
              "usage_update": "#/$defs/UsageUpdateData",
              "workspace_snapshot": "#/$defs/WorkspaceSnapshotData"
            },
            "propertyName": "kind"
          },
          "oneOf": [
            {
              "$ref": "#/$defs/ChatData"
            },
            {
              "$ref": "#/$defs/ChatThreadCreatedData"
            },
            {
              "$ref": "#/$defs/InvocationStartedData"
            },
            {
              "$ref": "#/$defs/InvocationFinishedData"
            },
            {
              "$ref": "#/$defs/AgentExecutionStartedData"
            },
            {
              "$ref": "#/$defs/AgentExecutionActivityData"
            },
            {
              "$ref": "#/$defs/AgentExecutionFinishedData"
            },
            {
              "$ref": "#/$defs/OutputData"
            },
            {
              "$ref": "#/$defs/ServerReadyData"
            },
            {
              "$ref": "#/$defs/RunStartedData"
            },
            {
              "$ref": "#/$defs/RunFailedData"
            },
            {
              "$ref": "#/$defs/RunInterruptedData"
            },
            {
              "$ref": "#/$defs/RunStatusChangedData"
            },
            {
              "$ref": "#/$defs/ExperimentsChangedData"
            },
            {
              "$ref": "#/$defs/ConfigurationFailedData"
            },
            {
              "$ref": "#/$defs/PhaseData"
            },
            {
              "$ref": "#/$defs/AgentOutputChunkData"
            },
            {
              "$ref": "#/$defs/SubprocessOutputData"
            },
            {
              "$ref": "#/$defs/JudgeResultData"
            },
            {
              "$ref": "#/$defs/BenchmarkResultData"
            },
            {
              "$ref": "#/$defs/RoundFinishedData"
            },
            {
              "$ref": "#/$defs/ToolCallData"
            },
            {
              "$ref": "#/$defs/ToolResultData"
            },
            {
              "$ref": "#/$defs/TodoUpdateData"
            },
            {
              "$ref": "#/$defs/UsageUpdateData"
            },
            {
              "$ref": "#/$defs/GateStartedData"
            },
            {
              "$ref": "#/$defs/GateFinishedData"
            },
            {
              "$ref": "#/$defs/WorkspaceSnapshotData"
            },
            {
              "$ref": "#/$defs/RunConfiguredData"
            },
            {
              "$ref": "#/$defs/FrameworkWarningData"
            }
          ]
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Data"
    }
  },
  "required": [
    "timestamp",
    "type"
  ],
  "title": "RunEvent",
  "type": "object"
};

export default runEventSchema;
