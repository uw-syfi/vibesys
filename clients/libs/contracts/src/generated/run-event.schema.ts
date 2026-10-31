/* Generated from the Python protocol models. Do not edit. */

const runEventSchema: Record<string, unknown> = {
  "$defs": {
    "AgentExecutionActivityData": {
      "additionalProperties": false,
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
      "additionalProperties": false,
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
        },
        "reply": {
          "anyOf": [
            {
              "$ref": "#/$defs/AgentReply"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        }
      },
      "title": "AgentExecutionFinishedData",
      "type": "object",
      "required": [
        "kind"
      ]
    },
    "AgentExecutionStartedData": {
      "additionalProperties": false,
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
        },
        "reasoning_effort": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Reasoning Effort"
        },
        "harness_version": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Harness Version"
        },
        "client": {
          "anyOf": [
            {
              "enum": [
                "scripted",
                "provider"
              ],
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Client"
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
      "additionalProperties": false,
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
    "AgentReply": {
      "additionalProperties": false,
      "description": "The typed reply a structured turn ended with, read through the role's own reply model.\n\n``kind`` is that model's name and ``json_schema`` its JSON Schema, so a reader learns which\nfields are text, choices or lists from the model and keeps no list of its own. ``value`` is\nthe validated reply as JSON.",
      "properties": {
        "kind": {
          "minLength": 1,
          "title": "Kind",
          "type": "string"
        },
        "json_schema": {
          "additionalProperties": true,
          "title": "Json Schema",
          "type": "object"
        },
        "value": {
          "additionalProperties": true,
          "title": "Value",
          "type": "object"
        }
      },
      "required": [
        "kind",
        "json_schema",
        "value"
      ],
      "title": "AgentReply",
      "type": "object"
    },
    "AgentStatusData": {
      "additionalProperties": false,
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
    "AttemptOutcome": {
      "description": "How one try of ``ctx.retry`` ended. Closed.",
      "enum": [
        "done",
        "again",
        "gave_up"
      ],
      "title": "AttemptOutcome",
      "type": "string"
    },
    "BenchmarkResultData": {
      "additionalProperties": false,
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
    "BenchmarkVersions": {
      "additionalProperties": false,
      "description": "What measured the run.\n\n``evaluator`` is the evaluator package when the task pins one; a source-tree evaluator has\nno version, so it is None.",
      "properties": {
        "evaluator": {
          "anyOf": [
            {
              "$ref": "#/$defs/EvaluatorIdentity"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        }
      },
      "title": "BenchmarkVersions",
      "type": "object"
    },
    "ChatData": {
      "additionalProperties": false,
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
      "additionalProperties": false,
      "description": "Identity and resolved agent settings for one experiment-chat thread.\n\nReplayed by clients to rebuild the thread list; the default thread is\nimplicit and never records one of these.",
      "properties": {
        "kind": {
          "const": "chat_thread_created",
          "default": "chat_thread_created",
          "title": "Kind",
          "type": "string"
        },
        "thread_id": {
          "$ref": "#/$defs/ChatThreadId"
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
    "ChatThreadId": {
      "maxLength": 128,
      "minLength": 1,
      "pattern": "^(?!(?:aux|con|nul|prn|com[1-9]|lpt[1-9])(?:\\.|$))(?!.*\\.$)[a-z0-9][a-z0-9._-]*$(?![\\s\\S])",
      "type": "string"
    },
    "CheckoutStatus": {
      "description": "Why the optional checkout update advanced or skipped the checkout.",
      "enum": [
        "fast_forwarded",
        "other_branch",
        "dirty_tree",
        "diverged",
        "git_refused",
        "operator_changed_checkout"
      ],
      "title": "CheckoutStatus",
      "type": "string"
    },
    "CommandResultPayload": {
      "additionalProperties": false,
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
    "ComparisonReadingData": {
      "additionalProperties": false,
      "description": "One comparison baseline as the run read it from the project's store.\n\n``value`` is the mean of ``samples`` runs of the task's benchmark against the engine, and\n``spread`` their relative standard deviation (absent for one run). ``reason`` explains a\nfailed or unconfigured comparison. A hidden-from-agents comparison is still shown here:\nthe operator sees it; only the planner's prompt omits it.",
      "properties": {
        "name": {
          "title": "Name",
          "type": "string"
        },
        "label": {
          "title": "Label",
          "type": "string"
        },
        "status": {
          "$ref": "#/$defs/ComparisonStatus"
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
        "direction": {
          "anyOf": [
            {
              "enum": [
                "max",
                "min"
              ],
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Direction"
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
        "spread": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Spread"
        },
        "samples": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Samples"
        },
        "hosts": {
          "default": [],
          "items": {
            "type": "string"
          },
          "title": "Hosts",
          "type": "array"
        },
        "reason": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Reason"
        }
      },
      "required": [
        "name",
        "label",
        "status"
      ],
      "title": "ComparisonReadingData",
      "type": "object"
    },
    "ComparisonStatus": {
      "description": "What a run knows of one comparison baseline. Closed; mirrored by ``server.events``.",
      "enum": [
        "measured",
        "failed",
        "not_configured",
        "not_measured"
      ],
      "title": "ComparisonStatus",
      "type": "string"
    },
    "ComparisonsReadData": {
      "additionalProperties": false,
      "description": "The comparison baselines a run read when it started or resumed; the newest event wins.\n\nA run never measures a comparison: ``vibesys compare`` does, once, and stores the reading.",
      "properties": {
        "kind": {
          "const": "comparisons_read",
          "default": "comparisons_read",
          "title": "Kind",
          "type": "string"
        },
        "comparisons": {
          "items": {
            "$ref": "#/$defs/ComparisonReadingData"
          },
          "title": "Comparisons",
          "type": "array"
        }
      },
      "required": [
        "kind",
        "comparisons"
      ],
      "title": "ComparisonsReadData",
      "type": "object"
    },
    "ConfigurationFailedData": {
      "additionalProperties": false,
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
    "EndReason": {
      "description": "Why the framework ended the run as it did. Closed: the result's reason starts with it.",
      "enum": [
        "adopted",
        "kept-input",
        "no-winner",
        "adoption-failed",
        "stopped",
        "deadline",
        "failed",
        "finish-refused"
      ],
      "title": "EndReason",
      "type": "string"
    },
    "EvaluatorIdentity": {
      "additionalProperties": false,
      "description": "The evaluator package a run measures with, as the task's manifest pins it.",
      "properties": {
        "name": {
          "minLength": 1,
          "title": "Name",
          "type": "string"
        },
        "version": {
          "minLength": 1,
          "title": "Version",
          "type": "string"
        }
      },
      "required": [
        "name",
        "version"
      ],
      "title": "EvaluatorIdentity",
      "type": "object"
    },
    "EventStatus": {
      "description": "Lifecycle and command states reported in events.",
      "enum": [
        "active",
        "answered",
        "pending",
        "consumed",
        "delivered",
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
        "run_startup_progress",
        "workspace_root_probe",
        "experiments_changed",
        "iterations_changed",
        "run_interrupted",
        "run_status_changed",
        "chat",
        "chat_thread_created",
        "steer_proposed",
        "steer_delivery",
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
        "run_ending",
        "output",
        "tool_call",
        "tool_result",
        "todo_update",
        "usage_update",
        "rate_limit_update",
        "quota_paused",
        "quota_resumed",
        "quota_abandoned",
        "quota_waiting",
        "quota_wait_cancelled",
        "provider_switched",
        "gate_started",
        "gate_finished",
        "workspace_snapshot",
        "run_configured",
        "framework_warning",
        "winner_selected",
        "input_measurement_failed",
        "measurement_compared",
        "comparisons_read",
        "workstream_finished",
        "turn_usage_recorded",
        "span_started",
        "span_updated",
        "span_ended"
      ],
      "title": "EventType",
      "type": "string"
    },
    "ExperimentsChangedData": {
      "additionalProperties": false,
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
    "FailureKind": {
      "description": "flows.md's failure kinds a span can end with. Closed.\n\nA defect crashes the step that raised it, which is never persisted, so no trace holds one.",
      "enum": [
        "outcome",
        "interruption"
      ],
      "title": "FailureKind",
      "type": "string"
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
      "additionalProperties": false,
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
        },
        "new_process_count": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "New Process Count"
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
      "additionalProperties": false,
      "description": "Outcome of one framework gate; envelope status carries pass or fail.\n\n``metric``/``value``/``unit`` are set only on a passing benchmark gate.\n``unit`` keeps the historical fallback of the metric name when the\ncontract declares no unit. ``output_tail`` carries bounded, credential-redacted\ntrailing command output on failure. ``round_label`` groups the per-recipe local-validation\ngates of one attempt back together, so a client folds them into a\npass-fraction itself rather than the backend aggregating one.",
      "properties": {
        "kind": {
          "const": "gate_finished",
          "default": "gate_finished",
          "title": "Kind",
          "type": "string"
        },
        "span_id": {
          "anyOf": [
            {
              "$ref": "#/$defs/SpanId"
            },
            {
              "type": "null"
            }
          ],
          "default": null
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
        "build",
        "accuracy",
        "benchmark"
      ],
      "title": "GateKind",
      "type": "string"
    },
    "GateStartedData": {
      "additionalProperties": false,
      "description": "One framework gate began evaluating the current candidate.",
      "properties": {
        "kind": {
          "const": "gate_started",
          "default": "gate_started",
          "title": "Kind",
          "type": "string"
        },
        "span_id": {
          "anyOf": [
            {
              "$ref": "#/$defs/SpanId"
            },
            {
              "type": "null"
            }
          ],
          "default": null
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
    "InputMeasurementFailedData": {
      "additionalProperties": false,
      "description": "The run's input failed its trusted measurement, so the run has no trusted baseline.\n\nMirrors ``vibesys.events.InputMeasurementFailedData`` field for field.",
      "properties": {
        "kind": {
          "const": "input_measurement_failed",
          "default": "input_measurement_failed",
          "title": "Kind",
          "type": "string"
        },
        "failure": {
          "$ref": "#/$defs/InputMeasurementFailureKind"
        },
        "stage": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Stage"
        },
        "summary": {
          "default": "",
          "title": "Summary",
          "type": "string"
        },
        "attempts": {
          "default": 1,
          "minimum": 1,
          "title": "Attempts",
          "type": "integer"
        },
        "compared": {
          "default": false,
          "title": "Compared",
          "type": "boolean"
        }
      },
      "required": [
        "kind",
        "failure"
      ],
      "title": "InputMeasurementFailedData",
      "type": "object"
    },
    "InputMeasurementFailureKind": {
      "description": "Why the run's input failed its own trusted measurement. Closed.\n\nSee ``vibesys.events.InputMeasurementFailureKind``, which this mirrors member-for-member.",
      "enum": [
        "no-evidence",
        "readings-refused",
        "readings-undecodable",
        "accuracy-failed",
        "benchmark-failed",
        "wait-ended"
      ],
      "title": "InputMeasurementFailureKind",
      "type": "string"
    },
    "InvocationFinishedData": {
      "additionalProperties": false,
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
      "additionalProperties": false,
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
    "IterationsChangedData": {
      "additionalProperties": false,
      "description": "Semantic revision of the policy-owned iteration projection.",
      "properties": {
        "kind": {
          "const": "iterations_changed",
          "default": "iterations_changed",
          "title": "Kind",
          "type": "string"
        },
        "revision": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Revision"
        }
      },
      "title": "IterationsChangedData",
      "type": "object",
      "required": [
        "kind"
      ]
    },
    "JsonResultPayload": {
      "additionalProperties": false,
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
      "additionalProperties": false,
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
    "MeasurementComparedData": {
      "additionalProperties": false,
      "description": "A candidate's measurement was compared with its base's. See ``vibesys.events``.",
      "properties": {
        "kind": {
          "const": "measurement_compared",
          "default": "measurement_compared",
          "title": "Kind",
          "type": "string"
        },
        "metric": {
          "title": "Metric",
          "type": "string"
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
        "direction": {
          "enum": [
            "max",
            "min"
          ],
          "title": "Direction",
          "type": "string"
        },
        "candidate_commit": {
          "title": "Candidate Commit",
          "type": "string"
        },
        "base_commit": {
          "title": "Base Commit",
          "type": "string"
        },
        "candidate_value": {
          "title": "Candidate Value",
          "type": "number"
        },
        "base_value": {
          "title": "Base Value",
          "type": "number"
        },
        "ratio": {
          "title": "Ratio",
          "type": "number"
        },
        "noise": {
          "title": "Noise",
          "type": "number"
        },
        "verdict": {
          "$ref": "#/$defs/MeasurementVerdict"
        },
        "candidate_samples": {
          "title": "Candidate Samples",
          "type": "integer"
        },
        "base_samples": {
          "title": "Base Samples",
          "type": "integer"
        },
        "source": {
          "$ref": "#/$defs/FrameworkSource",
          "default": "gates"
        }
      },
      "required": [
        "kind",
        "metric",
        "direction",
        "candidate_commit",
        "base_commit",
        "candidate_value",
        "base_value",
        "ratio",
        "noise",
        "verdict",
        "candidate_samples",
        "base_samples"
      ],
      "title": "MeasurementComparedData",
      "type": "object"
    },
    "MeasurementVerdict": {
      "description": "Which side of the noise a candidate falls on. Mirrors ``vs_core.api.ComparisonVerdict``.",
      "enum": [
        "better",
        "within_noise",
        "worse",
        "inconclusive"
      ],
      "title": "MeasurementVerdict",
      "type": "string"
    },
    "ModelTurnUsageData": {
      "additionalProperties": false,
      "description": "One model's share of one finished turn. See ``vibesys.events.ModelTurnUsageData``.",
      "properties": {
        "model": {
          "minLength": 1,
          "title": "Model",
          "type": "string"
        },
        "input_tokens": {
          "minimum": 0,
          "title": "Input Tokens",
          "type": "integer"
        },
        "cache_creation_input_tokens": {
          "minimum": 0,
          "title": "Cache Creation Input Tokens",
          "type": "integer"
        },
        "cache_creation_1h_input_tokens": {
          "minimum": 0,
          "title": "Cache Creation 1H Input Tokens",
          "type": "integer"
        },
        "cache_read_input_tokens": {
          "minimum": 0,
          "title": "Cache Read Input Tokens",
          "type": "integer"
        },
        "output_tokens": {
          "minimum": 0,
          "title": "Output Tokens",
          "type": "integer"
        },
        "reasoning_output_tokens": {
          "minimum": 0,
          "title": "Reasoning Output Tokens",
          "type": "integer"
        },
        "turns": {
          "minimum": 0,
          "title": "Turns",
          "type": "integer"
        },
        "recorded_usd": {
          "anyOf": [
            {
              "minimum": 0,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Recorded Usd"
        },
        "speed": {
          "anyOf": [
            {
              "enum": [
                "standard",
                "fast",
                "ultrafast"
              ],
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Speed"
        },
        "primary": {
          "default": false,
          "title": "Primary",
          "type": "boolean"
        }
      },
      "required": [
        "model",
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_creation_1h_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "turns"
      ],
      "title": "ModelTurnUsageData",
      "type": "object"
    },
    "ObjectiveOverview": {
      "additionalProperties": false,
      "description": "What the run optimizes: the task's objective and the metric it is judged on.\n\n``unit`` and ``direction`` come from the run's own measurements and objectives once it has\nthem; ``correctness_check`` is the argv of the check a candidate must pass (empty when the\ntask gates on none).",
      "properties": {
        "text": {
          "title": "Text",
          "type": "string"
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
        "direction": {
          "anyOf": [
            {
              "enum": [
                "max",
                "min"
              ],
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Direction"
        },
        "objectives": {
          "default": [],
          "items": {
            "$ref": "#/$defs/TaskObjective"
          },
          "title": "Objectives",
          "type": "array"
        },
        "correctness_check": {
          "default": [],
          "items": {
            "type": "string"
          },
          "title": "Correctness Check",
          "type": "array"
        }
      },
      "required": [
        "text"
      ],
      "title": "ObjectiveOverview",
      "type": "object"
    },
    "OutputData": {
      "additionalProperties": false,
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
      "additionalProperties": false,
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
    "ProviderSwitchedData": {
      "additionalProperties": false,
      "description": "The run replaced a provider by its fallback.\n\nSessions opened from now on run on ``to_provider`` and ``to_model`` as fresh\nconversations; a session already open does not move. Every later event\ncarries the provider and model it actually ran on.",
      "properties": {
        "kind": {
          "const": "provider_switched",
          "default": "provider_switched",
          "title": "Kind",
          "type": "string"
        },
        "from_provider": {
          "title": "From Provider",
          "type": "string"
        },
        "to_provider": {
          "title": "To Provider",
          "type": "string"
        },
        "to_model": {
          "title": "To Model",
          "type": "string"
        },
        "reason": {
          "enum": [
            "policy",
            "operator"
          ],
          "title": "Reason",
          "type": "string"
        },
        "detail": {
          "title": "Detail",
          "type": "string"
        }
      },
      "required": [
        "kind",
        "from_provider",
        "to_provider",
        "to_model",
        "reason",
        "detail"
      ],
      "title": "ProviderSwitchedData",
      "type": "object"
    },
    "QuotaAbandonedData": {
      "additionalProperties": false,
      "description": "The quota policy ended a turn with the quota error instead of pausing or waiting longer.\n\n``waited_seconds`` is how long this last stop held the turn before it ended (zero when\nit ended without waiting, and in events recorded before the field existed).",
      "properties": {
        "kind": {
          "const": "quota_abandoned",
          "default": "quota_abandoned",
          "title": "Kind",
          "type": "string"
        },
        "provider": {
          "title": "Provider",
          "type": "string"
        },
        "condition": {
          "enum": [
            "quota_exhausted",
            "rate_limited",
            "unavailable"
          ],
          "title": "Condition",
          "type": "string"
        },
        "detail": {
          "title": "Detail",
          "type": "string"
        },
        "reason": {
          "title": "Reason",
          "type": "string"
        },
        "waited_seconds": {
          "default": 0.0,
          "title": "Waited Seconds",
          "type": "number"
        }
      },
      "required": [
        "kind",
        "provider",
        "condition",
        "detail",
        "reason"
      ],
      "title": "QuotaAbandonedData",
      "type": "object"
    },
    "QuotaPausedData": {
      "additionalProperties": false,
      "description": "A turn stopped on a provider capacity limit and the run paused for it.\n\n``condition`` is ``quota_exhausted`` (a usage, spend or billing limit),\n``rate_limited`` (sustained rate limiting) or ``unavailable`` (a provider outage:\nonly the stopped turn waits, under its outage budget, and the run is not paused);\n``detail`` is the provider's\ndiagnostic and ``resets_at`` the epoch second the provider said capacity\nreturns, when it said. The run stays paused until it resumes; ``resumes_at`` is the\nepoch second the run's quota policy resumes it by itself, ``None`` when only the\noperator can. ``policy`` is the unattended quota policy; the fallback fields name the\nprovider and model an operator may resume with, ``None`` when none is configured.",
      "properties": {
        "kind": {
          "const": "quota_paused",
          "default": "quota_paused",
          "title": "Kind",
          "type": "string"
        },
        "provider": {
          "title": "Provider",
          "type": "string"
        },
        "condition": {
          "enum": [
            "quota_exhausted",
            "rate_limited",
            "unavailable"
          ],
          "title": "Condition",
          "type": "string"
        },
        "detail": {
          "title": "Detail",
          "type": "string"
        },
        "resets_at": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Resets At"
        },
        "resumes_at": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Resumes At"
        },
        "policy": {
          "default": "pause",
          "enum": [
            "pause",
            "wait",
            "fail",
            "fallback"
          ],
          "title": "Policy",
          "type": "string"
        },
        "fallback_provider": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Fallback Provider"
        },
        "fallback_model": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Fallback Model"
        }
      },
      "required": [
        "kind",
        "provider",
        "condition",
        "detail"
      ],
      "title": "QuotaPausedData",
      "type": "object"
    },
    "QuotaResumedData": {
      "additionalProperties": false,
      "description": "A turn stopped on a capacity limit is sent again on the same provider.\n\n``condition`` is the limit it stopped on, as in ``quota_paused`` (``None`` in events\nrecorded before the field existed). ``waited_seconds`` is how long that stop held the\nturn: for ``unavailable`` the turn's own backoff, during which the rest of the run went\non; otherwise the run's pause.",
      "properties": {
        "kind": {
          "const": "quota_resumed",
          "default": "quota_resumed",
          "title": "Kind",
          "type": "string"
        },
        "provider": {
          "title": "Provider",
          "type": "string"
        },
        "condition": {
          "anyOf": [
            {
              "enum": [
                "quota_exhausted",
                "rate_limited",
                "unavailable"
              ],
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Condition"
        },
        "reason": {
          "default": "operator",
          "enum": [
            "operator",
            "wait_elapsed"
          ],
          "title": "Reason",
          "type": "string"
        },
        "waited_seconds": {
          "default": 0.0,
          "title": "Waited Seconds",
          "type": "number"
        }
      },
      "required": [
        "kind",
        "provider"
      ],
      "title": "QuotaResumedData",
      "type": "object"
    },
    "QuotaWaitCancelledData": {
      "additionalProperties": false,
      "description": "The operator ended a wait for capacity: ``resumed`` the run now, or ``paused`` it again.",
      "properties": {
        "kind": {
          "const": "quota_wait_cancelled",
          "default": "quota_wait_cancelled",
          "title": "Kind",
          "type": "string"
        },
        "provider": {
          "title": "Provider",
          "type": "string"
        },
        "reason": {
          "enum": [
            "resumed",
            "paused"
          ],
          "title": "Reason",
          "type": "string"
        }
      },
      "required": [
        "kind",
        "provider",
        "reason"
      ],
      "title": "QuotaWaitCancelledData",
      "type": "object"
    },
    "QuotaWaitingData": {
      "additionalProperties": false,
      "description": "At the operator's request, the run paused on a capacity limit resumes when capacity returns.\n\n``resumes_at`` is the epoch second it resumes (the provider's reset time plus a margin),\n``None`` when the provider gave no reset time and the run tries again after the quota\npolicy's retry interval. A later request replaces this one.",
      "properties": {
        "kind": {
          "const": "quota_waiting",
          "default": "quota_waiting",
          "title": "Kind",
          "type": "string"
        },
        "provider": {
          "title": "Provider",
          "type": "string"
        },
        "resumes_at": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Resumes At"
        }
      },
      "required": [
        "kind",
        "provider"
      ],
      "title": "QuotaWaitingData",
      "type": "object"
    },
    "RateLimitUpdateData": {
      "additionalProperties": false,
      "description": "One rate-limit window a provider reported, as semantic data.\n\n``exhausted`` is the resolved fact (the provider's own statement, else\nusage at or past 100%); the other fields are what the provider stated,\n``None`` when it did not state them. ``resets_at`` is epoch seconds.",
      "properties": {
        "kind": {
          "const": "rate_limit_update",
          "default": "rate_limit_update",
          "title": "Kind",
          "type": "string"
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
        "window": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Window"
        },
        "limit": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Limit"
        },
        "used_fraction": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Used Fraction"
        },
        "resets_at": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Resets At"
        },
        "window_minutes": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Window Minutes"
        },
        "exhausted": {
          "title": "Exhausted",
          "type": "boolean"
        }
      },
      "required": [
        "kind",
        "exhausted"
      ],
      "title": "RateLimitUpdateData",
      "type": "object"
    },
    "RoundFinishedData": {
      "additionalProperties": false,
      "description": "Wire summary of judge and performance outcomes for one recorded round.\n\nA projected dynamic implementation round carries a non-null envelope\n``round_label`` matching its accompanying ``workstream_finished`` event.\n``attempts`` is strategy-neutral; dynamic currently reports one and does\nnot use it as an implementer-turn count. The CLI glossary owns the public\nround, workstream, and attempt definitions.",
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
      "additionalProperties": false,
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
    "RunEndingData": {
      "additionalProperties": false,
      "description": "How a core run's durable record stands as the run ends, emitted once just before its end.\n\n``reason`` is core's ``EndReason`` when the run recorded a result, ``None`` when it ended\nwithout one (an error, or a stop that left work unsettled). ``resumable`` is whether a\nresume continues it: its record is not terminal. ``rounds_finished`` counts its\nworkstreams (rounds) that reached their end. ``summary`` is core's account of the result.\n``uncompared`` is whether the result adopted a candidate never compared with the input (it\nfailed its own measurement), however the run ended.",
      "properties": {
        "kind": {
          "const": "run_ending",
          "default": "run_ending",
          "title": "Kind",
          "type": "string"
        },
        "reason": {
          "anyOf": [
            {
              "$ref": "#/$defs/EndReason"
            },
            {
              "type": "null"
            }
          ]
        },
        "summary": {
          "default": "",
          "title": "Summary",
          "type": "string"
        },
        "resumable": {
          "title": "Resumable",
          "type": "boolean"
        },
        "rounds_finished": {
          "minimum": 0,
          "title": "Rounds Finished",
          "type": "integer"
        },
        "uncompared": {
          "default": false,
          "title": "Uncompared",
          "type": "boolean"
        }
      },
      "required": [
        "kind",
        "reason",
        "resumable",
        "rounds_finished"
      ],
      "title": "RunEndingData",
      "type": "object"
    },
    "RunFailedData": {
      "additionalProperties": false,
      "description": "Why a run failed, what partial result it preserved, and the counts behind it.",
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
      "description": "What a failed run did and why it stopped, as data; frontends choose the wording.\n\n``reason`` is the strategy's own account of the stop. ``workstreams_started`` counts\nattempts core admitted, against the ``workstream_budget`` the run was allowed.\n``candidates_kept`` counts settled candidates eligible for adoption.\n``adopted_candidate`` names the verified candidate revision core adopted even though the\nstrategy still reported a failed outcome. Its None default preserves replay of failure events\nrecorded before the adoption fact was added to the event contract.",
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
        },
        "adopted_candidate": {
          "anyOf": [
            {
              "minLength": 1,
              "pattern": "^\\S+$",
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Adopted Candidate"
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
      "description": "Why a run failed to meet its goal, whether or not it preserved a partial result.",
      "enum": [
        "budget_exhausted",
        "deadline",
        "no_result",
        "run_environment_unavailable"
      ],
      "title": "RunFailureKind",
      "type": "string"
    },
    "RunInterruptedData": {
      "additionalProperties": false,
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
    "RunOverview": {
      "additionalProperties": false,
      "description": "The run's objective and what measures it: the Overview's header facts.\n\nOne definition for the core event and the wire: the run's ``run_started`` event carries it,\nand the server's snapshot carries it with the unit and direction its measurements add.",
      "properties": {
        "overview_version": {
          "const": 1,
          "default": 1,
          "title": "Overview Version",
          "type": "integer"
        },
        "objective": {
          "$ref": "#/$defs/ObjectiveOverview"
        },
        "benchmarks": {
          "$ref": "#/$defs/BenchmarkVersions",
          "default": {
            "evaluator": null
          }
        }
      },
      "required": [
        "objective"
      ],
      "title": "RunOverview",
      "type": "object"
    },
    "RunStartedData": {
      "additionalProperties": false,
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
        },
        "overview": {
          "anyOf": [
            {
              "$ref": "#/$defs/RunOverview"
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
        "outer_loop",
        "input"
      ],
      "title": "RunStartedData",
      "type": "object"
    },
    "RunStartupProgressData": {
      "additionalProperties": false,
      "description": "A starting run's ``stage`` began, moved, waited, or finished.\n\nJournaled while the run acquires its resources, before it is ready. The\nrun's stage only moves forward. ``state`` is ``running`` when a stage begins\nand at each step of its ``progress`` (an image build's steps done of total,\nand the step now running), ``waiting`` while it is blocked on something\noutside the run host (``condition``, and the ``elapsed_seconds`` waited so\nfar), and ``finished`` with the ``elapsed_seconds`` the stage took. A stage\nthat fails journals no ``finished``; the run's failure says why it ended.",
      "properties": {
        "kind": {
          "const": "run_startup_progress",
          "default": "run_startup_progress",
          "title": "Kind",
          "type": "string"
        },
        "stage": {
          "$ref": "#/$defs/StartupStage"
        },
        "progress": {
          "anyOf": [
            {
              "$ref": "#/$defs/StartupProgress"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "state": {
          "$ref": "#/$defs/StartupState",
          "default": "running"
        },
        "condition": {
          "anyOf": [
            {
              "$ref": "#/$defs/StartupWaitCondition"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "elapsed_seconds": {
          "anyOf": [
            {
              "minimum": 0,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Elapsed Seconds"
        }
      },
      "required": [
        "kind",
        "stage"
      ],
      "title": "RunStartupProgressData",
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
      "additionalProperties": false,
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
    "Seconds": {
      "minimum": 0,
      "type": "number"
    },
    "ServerReadyData": {
      "additionalProperties": false,
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
    "Span": {
      "additionalProperties": false,
      "description": "One flow task or awaited operation of the run, on the run clock.",
      "properties": {
        "id": {
          "$ref": "#/$defs/SpanId"
        },
        "parent": {
          "anyOf": [
            {
              "$ref": "#/$defs/SpanId"
            },
            {
              "type": "null"
            }
          ]
        },
        "type": {
          "$ref": "#/$defs/SpanType"
        },
        "name": {
          "title": "Name",
          "type": "string"
        },
        "status": {
          "$ref": "#/$defs/SpanStatus"
        },
        "waiting_on": {
          "default": [],
          "items": {
            "$ref": "#/$defs/WaitingOn"
          },
          "title": "Waiting On",
          "type": "array"
        },
        "attempts": {
          "default": [],
          "items": {
            "$ref": "#/$defs/SpanAttempt"
          },
          "title": "Attempts",
          "type": "array"
        },
        "started_at": {
          "$ref": "#/$defs/Seconds"
        },
        "ended_at": {
          "anyOf": [
            {
              "$ref": "#/$defs/Seconds"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "failure": {
          "anyOf": [
            {
              "$ref": "#/$defs/SpanFailure"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "stop": {
          "anyOf": [
            {
              "$ref": "#/$defs/SpanStop"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "invocation": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Invocation"
        },
        "label": {
          "anyOf": [
            {
              "$ref": "#/$defs/SpanLabel"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        }
      },
      "required": [
        "id",
        "parent",
        "type",
        "name",
        "status",
        "started_at"
      ],
      "title": "Span",
      "type": "object"
    },
    "SpanAttempt": {
      "additionalProperties": false,
      "description": "One try of ``ctx.retry`` in the flow: try ``n`` of ``of``.\n\n``outcome`` is None while the try runs, and stays None when the flow ended inside it.",
      "properties": {
        "n": {
          "title": "N",
          "type": "integer"
        },
        "of": {
          "title": "Of",
          "type": "integer"
        },
        "started_at": {
          "$ref": "#/$defs/Seconds"
        },
        "ended_at": {
          "anyOf": [
            {
              "$ref": "#/$defs/Seconds"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "outcome": {
          "anyOf": [
            {
              "$ref": "#/$defs/AttemptOutcome"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "reason": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Reason"
        }
      },
      "required": [
        "n",
        "of",
        "started_at"
      ],
      "title": "SpanAttempt",
      "type": "object"
    },
    "SpanEndedData": {
      "additionalProperties": false,
      "description": "A span ended.",
      "properties": {
        "span": {
          "$ref": "#/$defs/WireSpan"
        },
        "kind": {
          "const": "span_ended",
          "default": "span_ended",
          "title": "Kind",
          "type": "string"
        }
      },
      "required": [
        "span",
        "kind"
      ],
      "title": "SpanEndedData",
      "type": "object"
    },
    "SpanFailure": {
      "additionalProperties": false,
      "description": "Why a span ended failed or stopped.",
      "properties": {
        "kind": {
          "$ref": "#/$defs/FailureKind"
        },
        "message": {
          "title": "Message",
          "type": "string"
        }
      },
      "required": [
        "kind",
        "message"
      ],
      "title": "SpanFailure",
      "type": "object"
    },
    "SpanId": {
      "pattern": "^(flow|op):\\S*$",
      "type": "string"
    },
    "SpanLabel": {
      "additionalProperties": false,
      "description": "What the search says a span is: one of its declared kinds, with that kind's fields.\n\n``attrs_json`` holds the kind's attribute model as JSON (the search's own model, which a\nstate schema cannot name); ``verdict`` is one of the kind's verdicts, apart from ``status``.",
      "properties": {
        "kind": {
          "title": "Kind",
          "type": "string"
        },
        "title": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Title"
        },
        "links": {
          "default": [],
          "items": {
            "$ref": "#/$defs/SpanLink"
          },
          "title": "Links",
          "type": "array"
        },
        "attrs_json": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Attrs Json"
        },
        "verdict": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Verdict"
        },
        "reason": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Reason"
        }
      },
      "required": [
        "kind"
      ],
      "title": "SpanLabel",
      "type": "object"
    },
    "SpanLink": {
      "additionalProperties": false,
      "description": "A labelled span's relation ``rel`` (one its kind declares) to the span ``to``.",
      "properties": {
        "rel": {
          "title": "Rel",
          "type": "string"
        },
        "to": {
          "$ref": "#/$defs/SpanId"
        }
      },
      "required": [
        "rel",
        "to"
      ],
      "title": "SpanLink",
      "type": "object"
    },
    "SpanStartedData": {
      "additionalProperties": false,
      "description": "A span started.",
      "properties": {
        "span": {
          "$ref": "#/$defs/WireSpan"
        },
        "kind": {
          "const": "span_started",
          "default": "span_started",
          "title": "Kind",
          "type": "string"
        }
      },
      "required": [
        "span",
        "kind"
      ],
      "title": "SpanStartedData",
      "type": "object"
    },
    "SpanStatus": {
      "description": "Where a span is in its lifecycle. Closed.",
      "enum": [
        "pending",
        "running",
        "waiting",
        "done",
        "failed",
        "stopped"
      ],
      "title": "SpanStatus",
      "type": "string"
    },
    "SpanStop": {
      "additionalProperties": false,
      "description": "The operator's stop as this flow saw it: when it arrived, and whether it interrupted it.",
      "properties": {
        "requested_at": {
          "$ref": "#/$defs/Seconds"
        },
        "landed": {
          "default": false,
          "title": "Landed",
          "type": "boolean"
        }
      },
      "required": [
        "requested_at"
      ],
      "title": "SpanStop",
      "type": "object"
    },
    "SpanType": {
      "description": "What a span stands for. Closed.",
      "enum": [
        "flow",
        "operation"
      ],
      "title": "SpanType",
      "type": "string"
    },
    "SpanUpdatedData": {
      "additionalProperties": false,
      "description": "A span changed.",
      "properties": {
        "span": {
          "$ref": "#/$defs/WireSpan"
        },
        "kind": {
          "const": "span_updated",
          "default": "span_updated",
          "title": "Kind",
          "type": "string"
        }
      },
      "required": [
        "span",
        "kind"
      ],
      "title": "SpanUpdatedData",
      "type": "object"
    },
    "SpanWaitKind": {
      "description": "What a flow waits on. Closed.",
      "enum": [
        "operation",
        "slot",
        "time",
        "stop"
      ],
      "title": "SpanWaitKind",
      "type": "string"
    },
    "StartupProgress": {
      "additionalProperties": false,
      "description": "How far a startup stage has come: ``done`` of ``total`` steps, now on ``label``.\n\n``total`` is ``None`` when the stage does not know it. ``label`` names the\nstep now running (an image build's Dockerfile instruction), as data.",
      "properties": {
        "done": {
          "minimum": 0,
          "title": "Done",
          "type": "integer"
        },
        "total": {
          "anyOf": [
            {
              "minimum": 1,
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Total"
        },
        "label": {
          "default": "",
          "title": "Label",
          "type": "string"
        }
      },
      "required": [
        "done"
      ],
      "title": "StartupProgress",
      "type": "object"
    },
    "StartupStage": {
      "description": "Where a starting run is, in the order it gets there.\n\nDeclaration order is the order: a starting run only moves to a later stage.",
      "enum": [
        "resolving",
        "probing_workspace",
        "building_image",
        "building_tools",
        "starting_container"
      ],
      "title": "StartupStage",
      "type": "string"
    },
    "StartupState": {
      "description": "What a startup stage is doing at the moment of one observation.",
      "enum": [
        "running",
        "waiting",
        "finished"
      ],
      "title": "StartupState",
      "type": "string"
    },
    "StartupWaitCondition": {
      "description": "What a waiting startup stage waits on.",
      "enum": [
        "queued",
        "running"
      ],
      "title": "StartupWaitCondition",
      "type": "string"
    },
    "SteerDelivery": {
      "additionalProperties": false,
      "description": "One step in a steer's life: the facts a client needs to show it, by control id.\n\n``control_id`` is the id the operator sent the steer or withdrawal under, so a client\nmatches the step to its own send; None for a control sent without one. A step carries\nthe facts its state names and no other: ``mode`` when queued, ``withdraws`` for a queued\nwithdrawal, ``role`` when a turn took the steer, and ``reason`` when it ended unreached.",
      "properties": {
        "state": {
          "$ref": "#/$defs/SteerDeliveryState"
        },
        "control_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Control Id"
        },
        "action": {
          "default": "steer",
          "enum": [
            "steer",
            "withdraw"
          ],
          "title": "Action",
          "type": "string"
        },
        "mode": {
          "anyOf": [
            {
              "$ref": "#/$defs/SteerMode"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "target": {
          "anyOf": [
            {
              "$ref": "#/$defs/SteerTarget"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "withdraws": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Withdraws"
        },
        "role": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Role"
        },
        "attempt_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Attempt Id"
        },
        "reason": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Reason"
        }
      },
      "required": [
        "state"
      ],
      "title": "SteerDelivery",
      "type": "object"
    },
    "SteerDeliveryData": {
      "additionalProperties": false,
      "description": "One step in an operator steer's life, by the control id the operator sent it under.\n\nCarried by the steer-lifecycle run-control events (queued, reached a turn, in force,\ndropped, ended, withdrawal queued, refused); ``SteerDelivery`` is its one definition.",
      "properties": {
        "kind": {
          "const": "steer_delivery",
          "default": "steer_delivery",
          "title": "Kind",
          "type": "string"
        },
        "delivery": {
          "$ref": "#/$defs/SteerDelivery"
        }
      },
      "required": [
        "kind",
        "delivery"
      ],
      "title": "SteerDeliveryData",
      "type": "object"
    },
    "SteerDeliveryState": {
      "description": "Where one operator steer or withdrawal stands, as a client shows it.",
      "enum": [
        "queued",
        "withdraw_queued",
        "reached",
        "reached_mid_turn",
        "in_force",
        "dropped",
        "ended",
        "refused"
      ],
      "title": "SteerDeliveryState",
      "type": "string"
    },
    "SteerMode": {
      "description": "How a steer reaches the turns of its target.",
      "enum": [
        "next_turn",
        "standing",
        "interrupt"
      ],
      "title": "SteerMode",
      "type": "string"
    },
    "SteerProposal": {
      "additionalProperties": false,
      "description": "Guidance a chat agent suggests, in the fields of a ``SteerRequest``.\n\nIt carries no ``control_id`` and nothing in it reaches the run: only an operator's\n``command.control`` with a ``SteerRequest`` does. The tool that returns it is the\nagent's only steering tool, so the agent cannot send guidance.",
      "properties": {
        "text": {
          "minLength": 1,
          "title": "Text",
          "type": "string"
        },
        "target": {
          "$ref": "#/$defs/SteerTarget",
          "default": {
            "kind": "run",
            "id": null
          }
        },
        "mode": {
          "$ref": "#/$defs/SteerMode",
          "default": "next_turn"
        }
      },
      "required": [
        "text"
      ],
      "title": "SteerProposal",
      "type": "object"
    },
    "SteerProposedData": {
      "additionalProperties": false,
      "description": "A steer the chat agent suggests; the operator edits, sends or dismisses it.\n\nRecorded on the chat thread (the event header's ``chat_thread_id``) whose turn called\n``propose_steer``. It is a proposal only: nothing reaches the run until the operator sends\na ``command.control`` steer, which the proposal's fields prefill. ``proposal_id`` names it\nfor the client's card, and is unique within the run.",
      "properties": {
        "kind": {
          "const": "steer_proposed",
          "default": "steer_proposed",
          "title": "Kind",
          "type": "string"
        },
        "proposal_id": {
          "minLength": 1,
          "title": "Proposal Id",
          "type": "string"
        },
        "proposal": {
          "$ref": "#/$defs/SteerProposal"
        }
      },
      "required": [
        "kind",
        "proposal_id",
        "proposal"
      ],
      "title": "SteerProposedData",
      "type": "object"
    },
    "SteerTarget": {
      "additionalProperties": false,
      "description": "Whom a steer is for: the whole run, or one role, attempt or item, by id.",
      "properties": {
        "kind": {
          "$ref": "#/$defs/SteerTargetKind",
          "default": "run"
        },
        "id": {
          "anyOf": [
            {
              "$ref": "#/$defs/SubjectId"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        }
      },
      "title": "SteerTarget",
      "type": "object"
    },
    "SteerTargetKind": {
      "description": "What a steer is addressed to.",
      "enum": [
        "run",
        "role",
        "attempt",
        "item"
      ],
      "title": "SteerTargetKind",
      "type": "string"
    },
    "SubjectId": {
      "maxLength": 256,
      "minLength": 1,
      "pattern": "^\\S+$",
      "type": "string"
    },
    "SubprocessOutputData": {
      "additionalProperties": false,
      "description": "Captured output from a managed subprocess.",
      "properties": {
        "kind": {
          "const": "subprocess_output",
          "default": "subprocess_output",
          "title": "Kind",
          "type": "string"
        },
        "span_id": {
          "anyOf": [
            {
              "$ref": "#/$defs/SpanId"
            },
            {
              "type": "null"
            }
          ],
          "default": null
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
    "TaskObjective": {
      "additionalProperties": false,
      "description": "One axis the task compares candidates on, and which way is better.",
      "properties": {
        "name": {
          "minLength": 1,
          "title": "Name",
          "type": "string"
        },
        "direction": {
          "enum": [
            "max",
            "min"
          ],
          "title": "Direction",
          "type": "string"
        }
      },
      "required": [
        "name",
        "direction"
      ],
      "title": "TaskObjective",
      "type": "object"
    },
    "TodoItemData": {
      "additionalProperties": false,
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
      "additionalProperties": false,
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
      "additionalProperties": false,
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
      "additionalProperties": false,
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
    "TurnUsageData": {
      "additionalProperties": false,
      "description": "Settled token and cost accounting for one finished agent turn.\n\nSee ``vibesys.events.TurnUsageData``.",
      "properties": {
        "kind": {
          "const": "turn_usage_recorded",
          "default": "turn_usage_recorded",
          "title": "Kind",
          "type": "string"
        },
        "input_tokens": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Input Tokens"
        },
        "cache_creation_input_tokens": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Cache Creation Input Tokens"
        },
        "cache_read_input_tokens": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Cache Read Input Tokens"
        },
        "output_tokens": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Output Tokens"
        },
        "reasoning_output_tokens": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Reasoning Output Tokens"
        },
        "total_cost_usd": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Total Cost Usd"
        },
        "duration_ms": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Duration Ms"
        },
        "span_id": {
          "anyOf": [
            {
              "$ref": "#/$defs/SpanId"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "cost_basis": {
          "anyOf": [
            {
              "enum": [
                "price_table",
                "provider_reported",
                "unknown_model",
                "not_reported"
              ],
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Cost Basis"
        },
        "by_model": {
          "default": [],
          "items": {
            "$ref": "#/$defs/ModelTurnUsageData"
          },
          "title": "By Model",
          "type": "array"
        },
        "provider_session_id": {
          "anyOf": [
            {
              "minLength": 1,
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Provider Session Id"
        }
      },
      "title": "TurnUsageData",
      "type": "object",
      "required": [
        "kind"
      ]
    },
    "UsageUpdateData": {
      "additionalProperties": false,
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
    "WaitingOn": {
      "additionalProperties": false,
      "description": "One source a suspended flow waits on, since the run-clock time it began waiting on it.",
      "properties": {
        "kind": {
          "$ref": "#/$defs/SpanWaitKind"
        },
        "ref": {
          "title": "Ref",
          "type": "string"
        },
        "since": {
          "$ref": "#/$defs/Seconds"
        }
      },
      "required": [
        "kind",
        "ref",
        "since"
      ],
      "title": "WaitingOn",
      "type": "object"
    },
    "WinnerSelectedData": {
      "additionalProperties": false,
      "description": "The run's result was chosen: a retained candidate, or the unchanged trusted input.",
      "properties": {
        "kind": {
          "const": "winner_selected",
          "default": "winner_selected",
          "title": "Kind",
          "type": "string"
        },
        "selection": {
          "enum": [
            "candidate",
            "baseline"
          ],
          "title": "Selection",
          "type": "string"
        },
        "revision": {
          "title": "Revision",
          "type": "string"
        },
        "settlement_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Settlement Id"
        },
        "checkout_status": {
          "anyOf": [
            {
              "$ref": "#/$defs/CheckoutStatus"
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
        "selection",
        "revision"
      ],
      "title": "WinnerSelectedData",
      "type": "object"
    },
    "WireSpan": {
      "$ref": "#/$defs/Span"
    },
    "WorkspaceRootProbeData": {
      "additionalProperties": false,
      "description": "The run's probe of its local Slurm workspace root, which runs beside the run.\n\nThe probe is a small Slurm job that proves the compute nodes share the root the run\nstages into. The run starts without it; each Slurm submission (a trusted evaluation,\nan agent's GPU job) waits for it. ``queued``: the probe job ``job_id`` still waits in\nthe scheduler's queue, for the scheduler's ``pending_reason`` (None when it gave\nnone), after ``waited_seconds`` of at most ``queue_timeout_seconds``; it is reported\nat once, then on a bounded cadence or when the reason changes. ``retrying``: an\nattempt ended with ``failure`` without saying anything about the root (the queue\nnever scheduled it), and attempt number ``attempt`` follows in ``retry_in_seconds``;\nSlurm work keeps waiting. ``proven``: Slurm work goes ahead. ``failed``: every Slurm\nsubmission of the run is refused with ``failure``, and the run ends as failed.",
      "properties": {
        "kind": {
          "const": "workspace_root_probe",
          "default": "workspace_root_probe",
          "title": "Kind",
          "type": "string"
        },
        "state": {
          "enum": [
            "queued",
            "retrying",
            "proven",
            "failed"
          ],
          "title": "State",
          "type": "string"
        },
        "job_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Job Id"
        },
        "pending_reason": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Pending Reason"
        },
        "waited_seconds": {
          "anyOf": [
            {
              "minimum": 0,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Waited Seconds"
        },
        "queue_timeout_seconds": {
          "anyOf": [
            {
              "minimum": 1,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Queue Timeout Seconds"
        },
        "failure": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Failure"
        },
        "attempt": {
          "anyOf": [
            {
              "minimum": 2,
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Attempt"
        },
        "retry_in_seconds": {
          "anyOf": [
            {
              "minimum": 0,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Retry In Seconds"
        }
      },
      "required": [
        "kind",
        "state"
      ],
      "title": "WorkspaceRootProbeData",
      "type": "object"
    },
    "WorkspaceSnapshotData": {
      "additionalProperties": false,
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
    },
    "WorkstreamFailureData": {
      "additionalProperties": false,
      "description": "One workstream's typed account of why it ended without a measured candidate.",
      "properties": {
        "kind": {
          "$ref": "#/$defs/WorkstreamFailureKind"
        },
        "role": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Role"
        },
        "turns": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Turns"
        },
        "limit": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Limit"
        },
        "text": {
          "default": "",
          "title": "Text",
          "type": "string"
        }
      },
      "required": [
        "kind"
      ],
      "title": "WorkstreamFailureData",
      "type": "object"
    },
    "WorkstreamFailureKind": {
      "description": "Why a workstream ended without a measured candidate. Closed.\n\nSee ``vibesys.events.WorkstreamFailureKind``, which this mirrors\nmember-for-member.",
      "enum": [
        "stopped",
        "parent-unverified",
        "refused",
        "ended",
        "out-of-budget",
        "turn-failed",
        "invalid-reply",
        "render-failed",
        "wait-refused",
        "checkpoint-declined",
        "no-changed-candidate",
        "implementation-failed",
        "review-rejected",
        "no-trusted-evidence",
        "evidence-refused",
        "evidence-undecodable",
        "profile-capture-unavailable",
        "turn-limit",
        "repeated-turn"
      ],
      "title": "WorkstreamFailureKind",
      "type": "string"
    },
    "WorkstreamFinishedData": {
      "additionalProperties": false,
      "description": "Wire summary for one projected, recorded policy round and its typed failure, if any.\n\nSee ``vibesys.events.WorkstreamFinishedData`` for the generic producer's\nserial and dynamic behavior; in the dynamic policy specifically, profile\nand merge workstreams do not emit this event.",
      "properties": {
        "kind": {
          "const": "workstream_finished",
          "default": "workstream_finished",
          "title": "Kind",
          "type": "string"
        },
        "repeated_error": {
          "title": "Repeated Error",
          "type": "boolean"
        },
        "last_error": {
          "anyOf": [
            {
              "$ref": "#/$defs/WorkstreamFailureData"
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
        "repeated_error"
      ],
      "title": "WorkstreamFinishedData",
      "type": "object"
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
          "$ref": "#/$defs/ChatThreadId"
        },
        {
          "type": "null"
        }
      ],
      "default": null
    },
    "span_id": {
      "anyOf": [
        {
          "$ref": "#/$defs/SpanId"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "The agent-turn span of the run trace this event belongs to: the server stamps it on every event whose execution_id ran as a recorded agent-turn span (the turn's output, tool calls and results, activity and usage), so a client joins a transcript to the trace by id and never by invocation, name or time. Absent when the event is not part of a turn, or when it was recorded before its span was."
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
              "comparisons_read": "#/$defs/ComparisonsReadData",
              "configuration_failed": "#/$defs/ConfigurationFailedData",
              "experiments_changed": "#/$defs/ExperimentsChangedData",
              "framework_warning": "#/$defs/FrameworkWarningData",
              "gate_finished": "#/$defs/GateFinishedData",
              "gate_started": "#/$defs/GateStartedData",
              "input_measurement_failed": "#/$defs/InputMeasurementFailedData",
              "invocation_finished": "#/$defs/InvocationFinishedData",
              "invocation_started": "#/$defs/InvocationStartedData",
              "iterations_changed": "#/$defs/IterationsChangedData",
              "judge_result": "#/$defs/JudgeResultData",
              "measurement_compared": "#/$defs/MeasurementComparedData",
              "output": "#/$defs/OutputData",
              "phase": "#/$defs/PhaseData",
              "provider_switched": "#/$defs/ProviderSwitchedData",
              "quota_abandoned": "#/$defs/QuotaAbandonedData",
              "quota_paused": "#/$defs/QuotaPausedData",
              "quota_resumed": "#/$defs/QuotaResumedData",
              "quota_wait_cancelled": "#/$defs/QuotaWaitCancelledData",
              "quota_waiting": "#/$defs/QuotaWaitingData",
              "rate_limit_update": "#/$defs/RateLimitUpdateData",
              "round_finished": "#/$defs/RoundFinishedData",
              "run_configured": "#/$defs/RunConfiguredData",
              "run_ending": "#/$defs/RunEndingData",
              "run_failed": "#/$defs/RunFailedData",
              "run_interrupted": "#/$defs/RunInterruptedData",
              "run_started": "#/$defs/RunStartedData",
              "run_startup_progress": "#/$defs/RunStartupProgressData",
              "run_status_changed": "#/$defs/RunStatusChangedData",
              "server_ready": "#/$defs/ServerReadyData",
              "span_ended": "#/$defs/SpanEndedData",
              "span_started": "#/$defs/SpanStartedData",
              "span_updated": "#/$defs/SpanUpdatedData",
              "steer_delivery": "#/$defs/SteerDeliveryData",
              "steer_proposed": "#/$defs/SteerProposedData",
              "subprocess_output": "#/$defs/SubprocessOutputData",
              "todo_update": "#/$defs/TodoUpdateData",
              "tool_call": "#/$defs/ToolCallData",
              "tool_result": "#/$defs/ToolResultData",
              "turn_usage_recorded": "#/$defs/TurnUsageData",
              "usage_update": "#/$defs/UsageUpdateData",
              "winner_selected": "#/$defs/WinnerSelectedData",
              "workspace_root_probe": "#/$defs/WorkspaceRootProbeData",
              "workspace_snapshot": "#/$defs/WorkspaceSnapshotData",
              "workstream_finished": "#/$defs/WorkstreamFinishedData"
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
              "$ref": "#/$defs/RunStartupProgressData"
            },
            {
              "$ref": "#/$defs/RunFailedData"
            },
            {
              "$ref": "#/$defs/RunEndingData"
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
              "$ref": "#/$defs/IterationsChangedData"
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
              "$ref": "#/$defs/SteerProposedData"
            },
            {
              "$ref": "#/$defs/SteerDeliveryData"
            },
            {
              "$ref": "#/$defs/TodoUpdateData"
            },
            {
              "$ref": "#/$defs/UsageUpdateData"
            },
            {
              "$ref": "#/$defs/RateLimitUpdateData"
            },
            {
              "$ref": "#/$defs/QuotaPausedData"
            },
            {
              "$ref": "#/$defs/QuotaResumedData"
            },
            {
              "$ref": "#/$defs/QuotaAbandonedData"
            },
            {
              "$ref": "#/$defs/QuotaWaitingData"
            },
            {
              "$ref": "#/$defs/QuotaWaitCancelledData"
            },
            {
              "$ref": "#/$defs/WorkspaceRootProbeData"
            },
            {
              "$ref": "#/$defs/ProviderSwitchedData"
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
            },
            {
              "$ref": "#/$defs/WinnerSelectedData"
            },
            {
              "$ref": "#/$defs/InputMeasurementFailedData"
            },
            {
              "$ref": "#/$defs/MeasurementComparedData"
            },
            {
              "$ref": "#/$defs/ComparisonsReadData"
            },
            {
              "$ref": "#/$defs/WorkstreamFinishedData"
            },
            {
              "$ref": "#/$defs/TurnUsageData"
            },
            {
              "$ref": "#/$defs/SpanStartedData"
            },
            {
              "$ref": "#/$defs/SpanUpdatedData"
            },
            {
              "$ref": "#/$defs/SpanEndedData"
            }
          ]
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Data"
    },
    "truncated": {
      "default": false,
      "description": "True when the recording boundary cut variable-size payload fields to keep this event within MAX_SERIALIZED_RUN_EVENT_BYTES.",
      "title": "Truncated",
      "type": "boolean"
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
