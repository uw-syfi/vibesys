/* Generated from the Python protocol models. Do not edit. */

const hostHelperSchema: Record<string, unknown> = {
  "$defs": {
    "Accelerator": {
      "description": "How a machine's GPUs run work; a closed set, ``cpu`` when it has none.",
      "enum": [
        "cpu",
        "cuda",
        "rocm",
        "metal"
      ],
      "title": "Accelerator",
      "type": "string"
    },
    "AvailableLogTail": {
      "additionalProperties": false,
      "description": "A bounded suffix of one instance's regular ``server.log`` file.",
      "properties": {
        "state": {
          "const": "available",
          "default": "available",
          "title": "State",
          "type": "string"
        },
        "text": {
          "title": "Text",
          "type": "string",
          "x-vibesys-max-utf8-bytes": 65536
        },
        "truncated": {
          "title": "Truncated",
          "type": "boolean"
        }
      },
      "required": [
        "state",
        "text",
        "truncated"
      ],
      "title": "AvailableLogTail",
      "type": "object"
    },
    "ChannelEnd": {
      "additionalProperties": false,
      "description": "The one stderr line a relayed run channel writes when it ends with a nonzero status.",
      "properties": {
        "outcome": {
          "$ref": "#/$defs/ChannelEndOutcome"
        },
        "exit_status": {
          "title": "Exit Status",
          "type": "integer"
        },
        "detail": {
          "title": "Detail",
          "type": "string"
        }
      },
      "required": [
        "outcome",
        "exit_status",
        "detail"
      ],
      "title": "ChannelEnd",
      "type": "object"
    },
    "ChannelEndOutcome": {
      "description": "How a run channel's relay ended; each value has its own exit status.\n\nThe relay core (``server.relay``) decides the outcome and maps it to the\nhelper's exit status; a client reads it from the ``ChannelEnd`` line.",
      "enum": [
        "client_closed",
        "server_closed",
        "run_gone",
        "connect_denied",
        "connect_failed",
        "server_stalled",
        "client_stalled",
        "server_failed",
        "client_failed",
        "server_unresponsive"
      ],
      "title": "ChannelEndOutcome",
      "type": "string"
    },
    "Checkout": {
      "additionalProperties": false,
      "description": "The VibeSys source checkout the helper runs from.",
      "properties": {
        "root": {
          "title": "Root",
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
          "title": "Commit"
        },
        "dirty": {
          "anyOf": [
            {
              "type": "boolean"
            },
            {
              "type": "null"
            }
          ],
          "title": "Dirty"
        }
      },
      "required": [
        "root",
        "commit",
        "dirty"
      ],
      "title": "Checkout",
      "type": "object"
    },
    "CheckoutFound": {
      "additionalProperties": false,
      "description": "A VibeSys checkout at least ``MIN_CHECKOUT_VERSION``.\n\n``compatible`` says whether it ships the host helper this app starts from a checkout.",
      "properties": {
        "state": {
          "const": "found",
          "default": "found",
          "title": "State",
          "type": "string"
        },
        "root": {
          "title": "Root",
          "type": "string"
        },
        "version": {
          "title": "Version",
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
          "title": "Commit"
        },
        "dirty": {
          "anyOf": [
            {
              "type": "boolean"
            },
            {
              "type": "null"
            }
          ],
          "title": "Dirty"
        },
        "compatible": {
          "title": "Compatible",
          "type": "boolean"
        }
      },
      "required": [
        "state",
        "root",
        "version",
        "commit",
        "dirty",
        "compatible"
      ],
      "title": "CheckoutFound",
      "type": "object"
    },
    "CheckoutInspectReply": {
      "additionalProperties": false,
      "description": "What the path holds.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "checkout.inspect",
          "default": "checkout.inspect",
          "title": "Type",
          "type": "string"
        },
        "inspection": {
          "discriminator": {
            "mapping": {
              "found": "#/$defs/CheckoutFound",
              "not_found": "#/$defs/CheckoutNotFound",
              "too_old": "#/$defs/CheckoutTooOld"
            },
            "propertyName": "state"
          },
          "oneOf": [
            {
              "$ref": "#/$defs/CheckoutFound"
            },
            {
              "$ref": "#/$defs/CheckoutNotFound"
            },
            {
              "$ref": "#/$defs/CheckoutTooOld"
            }
          ],
          "title": "Inspection"
        }
      },
      "required": [
        "request_id",
        "type",
        "inspection"
      ],
      "title": "CheckoutInspectReply",
      "type": "object"
    },
    "CheckoutInspectRequest": {
      "additionalProperties": false,
      "description": "Whether ``path`` holds a VibeSys checkout, and whether this app can use it.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "checkout.inspect",
          "default": "checkout.inspect",
          "title": "Type",
          "type": "string"
        },
        "path": {
          "maxLength": 4096,
          "minLength": 1,
          "title": "Path",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "path"
      ],
      "title": "CheckoutInspectRequest",
      "type": "object"
    },
    "CheckoutNotFound": {
      "additionalProperties": false,
      "description": "The path holds no VibeSys checkout (or does not exist).",
      "properties": {
        "state": {
          "const": "not_found",
          "default": "not_found",
          "title": "State",
          "type": "string"
        }
      },
      "title": "CheckoutNotFound",
      "type": "object",
      "required": [
        "state"
      ]
    },
    "CheckoutTooOld": {
      "additionalProperties": false,
      "description": "A VibeSys checkout older than this app supports: ``version`` is below ``min``.",
      "properties": {
        "state": {
          "const": "too_old",
          "default": "too_old",
          "title": "State",
          "type": "string"
        },
        "version": {
          "title": "Version",
          "type": "string"
        },
        "min": {
          "title": "Min",
          "type": "string"
        }
      },
      "required": [
        "state",
        "version",
        "min"
      ],
      "title": "CheckoutTooOld",
      "type": "object"
    },
    "DetachedLaunchFailure": {
      "additionalProperties": false,
      "description": "What ``vibesys --detach`` prints on stdout, as one line, when it starts nothing.\n\nOn success the line is a ``LiveInstanceRecord`` instead; a reader tells them\napart by ``outcome``, which a record never has. ``code`` is a\n``DetachedLaunchErrorCode`` or the code of the run's configuration\ndiagnostic (``invalid_arguments``, ``resume_not_found``, ...), and\n``exit_code`` is the process's exit status.",
      "properties": {
        "version": {
          "const": 1,
          "default": 1,
          "title": "Version",
          "type": "integer"
        },
        "outcome": {
          "const": "failed",
          "default": "failed",
          "title": "Outcome",
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
        "exit_code": {
          "exclusiveMinimum": 0,
          "title": "Exit Code",
          "type": "integer"
        },
        "log_path": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Log Path"
        },
        "live_instance": {
          "anyOf": [
            {
              "$ref": "#/$defs/LiveInstanceRecord"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        }
      },
      "required": [
        "code",
        "stage",
        "message",
        "exit_code"
      ],
      "title": "DetachedLaunchFailure",
      "type": "object"
    },
    "DockerReachable": {
      "additionalProperties": false,
      "description": "Docker's daemon answered, with its server version.",
      "properties": {
        "reachable": {
          "const": true,
          "default": true,
          "title": "Reachable",
          "type": "boolean"
        },
        "version": {
          "title": "Version",
          "type": "string"
        }
      },
      "required": [
        "version"
      ],
      "title": "DockerReachable",
      "type": "object"
    },
    "DockerUnreachable": {
      "additionalProperties": false,
      "description": "Docker could not be reached, and why, in one line.",
      "properties": {
        "reachable": {
          "const": false,
          "default": false,
          "title": "Reachable",
          "type": "boolean"
        },
        "reason": {
          "title": "Reason",
          "type": "string"
        }
      },
      "required": [
        "reason"
      ],
      "title": "DockerUnreachable",
      "type": "object"
    },
    "EndRecord": {
      "additionalProperties": false,
      "description": "How a run ended, as its tombstone reports it: the sidebar's Recent row and the ended banner.\n\n``resumable`` says whether a resume continues the run: its durable record exists and\nis not terminal. ``resume_from_round`` is the round that resume starts at, when the\nserver knew it (a crashed server did not say).",
      "properties": {
        "outcome": {
          "$ref": "#/$defs/RunEndOutcome"
        },
        "reason": {
          "maxLength": 200,
          "minLength": 1,
          "pattern": "^[^\\n\\r]*$",
          "title": "Reason",
          "type": "string"
        },
        "resumable": {
          "title": "Resumable",
          "type": "boolean"
        },
        "resume_from_round": {
          "anyOf": [
            {
              "exclusiveMinimum": 0,
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Resume From Round"
        }
      },
      "required": [
        "outcome",
        "reason",
        "resumable"
      ],
      "title": "EndRecord",
      "type": "object"
    },
    "FailureReason": {
      "additionalProperties": false,
      "description": "Why a run failed: the diagnostic it ended with.",
      "properties": {
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
        }
      },
      "required": [
        "code",
        "stage",
        "message"
      ],
      "title": "FailureReason",
      "type": "object"
    },
    "Gpu": {
      "additionalProperties": false,
      "description": "One GPU as its vendor's tool names it.",
      "properties": {
        "vendor": {
          "title": "Vendor",
          "type": "string"
        },
        "model": {
          "title": "Model",
          "type": "string"
        }
      },
      "required": [
        "vendor",
        "model"
      ],
      "title": "Gpu",
      "type": "object"
    },
    "Hardware": {
      "additionalProperties": false,
      "description": "The machine's processors.",
      "properties": {
        "cpu_cores": {
          "exclusiveMinimum": 0,
          "title": "Cpu Cores",
          "type": "integer"
        },
        "gpus": {
          "items": {
            "$ref": "#/$defs/Gpu"
          },
          "title": "Gpus",
          "type": "array"
        },
        "accelerator": {
          "$ref": "#/$defs/Accelerator"
        }
      },
      "required": [
        "cpu_cores",
        "gpus",
        "accelerator"
      ],
      "title": "Hardware",
      "type": "object"
    },
    "HarnessChoice": {
      "additionalProperties": false,
      "description": "One agent CLI provider and one model it runs.",
      "properties": {
        "provider": {
          "title": "Provider",
          "type": "string"
        },
        "model": {
          "title": "Model",
          "type": "string"
        }
      },
      "required": [
        "provider",
        "model"
      ],
      "title": "HarnessChoice",
      "type": "object"
    },
    "HarnessOption": {
      "additionalProperties": false,
      "description": "A vetted provider and model pair a run may start with.",
      "properties": {
        "provider": {
          "title": "Provider",
          "type": "string"
        },
        "model": {
          "title": "Model",
          "type": "string"
        },
        "default": {
          "type": "boolean",
          "title": "Default"
        }
      },
      "required": [
        "provider",
        "model",
        "default"
      ],
      "title": "HarnessOption",
      "type": "object"
    },
    "HarnessesListReply": {
      "additionalProperties": false,
      "description": "The vetted pairs, grouped by provider with each provider's default first.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "harnesses.list",
          "default": "harnesses.list",
          "title": "Type",
          "type": "string"
        },
        "harnesses": {
          "items": {
            "$ref": "#/$defs/HarnessOption"
          },
          "title": "Harnesses",
          "type": "array"
        }
      },
      "required": [
        "request_id",
        "type",
        "harnesses"
      ],
      "title": "HarnessesListReply",
      "type": "object"
    },
    "HarnessesListRequest": {
      "additionalProperties": false,
      "description": "The vetted provider and model pairs ``run.start``'s ``harness`` accepts.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "harnesses.list",
          "default": "harnesses.list",
          "title": "Type",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type"
      ],
      "title": "HarnessesListRequest",
      "type": "object"
    },
    "HelloReply": {
      "additionalProperties": false,
      "description": "The negotiated version and features, and what will start runs on this host.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "hello",
          "default": "hello",
          "title": "Type",
          "type": "string"
        },
        "protocol_version": {
          "exclusiveMinimum": 0,
          "title": "Protocol Version",
          "type": "integer"
        },
        "features": {
          "items": {
            "$ref": "#/$defs/HelperFeature"
          },
          "title": "Features",
          "type": "array"
        },
        "vibesys_version": {
          "title": "Vibesys Version",
          "type": "string"
        },
        "checkout": {
          "anyOf": [
            {
              "$ref": "#/$defs/Checkout"
            },
            {
              "type": "null"
            }
          ]
        }
      },
      "required": [
        "request_id",
        "type",
        "protocol_version",
        "features",
        "vibesys_version",
        "checkout"
      ],
      "title": "HelloReply",
      "type": "object"
    },
    "HelloRequest": {
      "additionalProperties": false,
      "description": "The first message on every channel: which versions and features the client speaks.\n\nIts shape never changes across protocol versions, so any helper can parse\nany client's ``hello`` and answer ``incompatible`` with a reason.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "hello",
          "default": "hello",
          "title": "Type",
          "type": "string"
        },
        "client_versions": {
          "maxItems": 2,
          "minItems": 2,
          "prefixItems": [
            {
              "exclusiveMinimum": 0,
              "type": "integer"
            },
            {
              "exclusiveMinimum": 0,
              "type": "integer"
            }
          ],
          "title": "Client Versions",
          "type": "array"
        },
        "features": {
          "default": [],
          "items": {
            "type": "string"
          },
          "title": "Features",
          "type": "array"
        },
        "app_version": {
          "title": "App Version",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "client_versions",
        "app_version"
      ],
      "title": "HelloRequest",
      "type": "object"
    },
    "HelperError": {
      "additionalProperties": false,
      "description": "A failed request, or a line that was not one (``request_id`` is ``None`` then).",
      "properties": {
        "type": {
          "const": "error",
          "default": "error",
          "title": "Type",
          "type": "string"
        },
        "request_id": {
          "anyOf": [
            {
              "maxLength": 128,
              "minLength": 1,
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "title": "Request Id"
        },
        "code": {
          "$ref": "#/$defs/HelperErrorCode"
        },
        "message": {
          "title": "Message",
          "type": "string"
        },
        "path": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Path"
        },
        "supported_versions": {
          "anyOf": [
            {
              "maxItems": 2,
              "minItems": 2,
              "prefixItems": [
                {
                  "exclusiveMinimum": 0,
                  "type": "integer"
                },
                {
                  "exclusiveMinimum": 0,
                  "type": "integer"
                }
              ],
              "type": "array"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Supported Versions"
        },
        "vibesys_version": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Vibesys Version"
        }
      },
      "required": [
        "type",
        "request_id",
        "code",
        "message"
      ],
      "title": "HelperError",
      "type": "object"
    },
    "HelperErrorCode": {
      "description": "Why a request failed; a closed set.",
      "enum": [
        "handshake_required",
        "incompatible",
        "invalid_request",
        "duplicate_request_id",
        "feature_not_negotiated",
        "deadline_exceeded",
        "channel_busy",
        "unknown_watch",
        "project_missing",
        "project_uninitialized",
        "task_invalid",
        "path_missing",
        "path_not_directory",
        "permission_denied",
        "run_gone",
        "connect_denied",
        "connect_failed",
        "unknown_instance",
        "unknown_log_stream",
        "log_store_corrupt",
        "unknown_run",
        "unvetted_harness",
        "internal"
      ],
      "title": "HelperErrorCode",
      "type": "string"
    },
    "HelperFeature": {
      "description": "Optional capabilities negotiated by ``hello``; a closed set per protocol version.\n\nA client calls a gated method only after ``hello`` returned its feature, so\na strict peer never sees a method it does not know.",
      "enum": [
        "instances_watch",
        "paths_complete",
        "instances_inspect",
        "logs",
        "logs_cancel",
        "harnesses",
        "logs_by_span",
        "run_history",
        "machine_describe",
        "checkout_inspect"
      ],
      "title": "HelperFeature",
      "type": "string"
    },
    "InspectionState": {
      "description": "What one coherent registry observation proved about an instance.",
      "enum": [
        "live",
        "ended",
        "unverified"
      ],
      "title": "InspectionState",
      "type": "string"
    },
    "InstanceInspection": {
      "additionalProperties": false,
      "description": "``vibesys instances inspect --json``: identity plus an optional diagnostic tail.",
      "properties": {
        "version": {
          "const": 1,
          "default": 1,
          "title": "Version",
          "type": "integer"
        },
        "id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Id",
          "type": "string"
        },
        "state": {
          "$ref": "#/$defs/InspectionState"
        },
        "record": {
          "anyOf": [
            {
              "$ref": "#/$defs/LiveInstanceRecord"
            },
            {
              "type": "null"
            }
          ]
        },
        "metadata": {
          "anyOf": [
            {
              "$ref": "#/$defs/InstanceInspectionMetadata"
            },
            {
              "type": "null"
            }
          ]
        },
        "control_socket_path": {
          "maxLength": 4096,
          "minLength": 1,
          "title": "Control Socket Path",
          "type": "string"
        },
        "server_log_path": {
          "maxLength": 4096,
          "minLength": 1,
          "title": "Server Log Path",
          "type": "string"
        },
        "log": {
          "anyOf": [
            {
              "discriminator": {
                "mapping": {
                  "available": "#/$defs/AvailableLogTail",
                  "missing": "#/$defs/MissingLogTail"
                },
                "propertyName": "state"
              },
              "oneOf": [
                {
                  "$ref": "#/$defs/AvailableLogTail"
                },
                {
                  "$ref": "#/$defs/MissingLogTail"
                }
              ]
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Log"
        }
      },
      "required": [
        "id",
        "state",
        "record",
        "metadata",
        "control_socket_path",
        "server_log_path"
      ],
      "title": "InstanceInspection",
      "type": "object"
    },
    "InstanceInspectionMetadata": {
      "additionalProperties": false,
      "description": "Immutable detached-process facts retained after its live record disappears.",
      "properties": {
        "version": {
          "const": 1,
          "default": 1,
          "title": "Version",
          "type": "integer"
        },
        "id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Id",
          "type": "string"
        },
        "project_root": {
          "maxLength": 4096,
          "minLength": 1,
          "title": "Project Root",
          "type": "string"
        },
        "run_id": {
          "pattern": "^[a-z0-9][a-z0-9._-]{0,127}$",
          "title": "Run Id",
          "type": "string"
        },
        "task": {
          "anyOf": [
            {
              "pattern": "^[a-z0-9][a-z0-9._-]{0,127}$",
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Task"
        },
        "pid": {
          "exclusiveMinimum": 0,
          "title": "Pid",
          "type": "integer"
        },
        "started_at": {
          "maximum": 8640000000000,
          "minimum": 0,
          "title": "Started At",
          "type": "number"
        },
        "hostname": {
          "maxLength": 255,
          "minLength": 1,
          "title": "Hostname",
          "type": "string"
        },
        "protocol_version": {
          "const": 1,
          "default": 1,
          "title": "Protocol Version",
          "type": "integer"
        },
        "vibesys_version": {
          "maxLength": 255,
          "minLength": 1,
          "title": "Vibesys Version",
          "type": "string"
        },
        "vibesys_root": {
          "anyOf": [
            {
              "maxLength": 4096,
              "minLength": 1,
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Vibesys Root"
        },
        "control_socket_path": {
          "maxLength": 4096,
          "minLength": 1,
          "title": "Control Socket Path",
          "type": "string"
        },
        "server_log_path": {
          "maxLength": 4096,
          "minLength": 1,
          "title": "Server Log Path",
          "type": "string"
        }
      },
      "required": [
        "id",
        "project_root",
        "run_id",
        "pid",
        "started_at",
        "hostname",
        "vibesys_version",
        "control_socket_path",
        "server_log_path"
      ],
      "title": "InstanceInspectionMetadata",
      "type": "object"
    },
    "InstanceList": {
      "additionalProperties": false,
      "description": "``vibesys instances list --json``: the detached servers on this node (version 5).",
      "properties": {
        "version": {
          "const": 5,
          "default": 5,
          "title": "Version",
          "type": "integer"
        },
        "instances": {
          "items": {
            "$ref": "#/$defs/LiveInstanceRecord"
          },
          "title": "Instances",
          "type": "array"
        },
        "ended": {
          "default": [],
          "items": {
            "$ref": "#/$defs/LiveInstanceRecord"
          },
          "title": "Ended",
          "type": "array"
        },
        "unreadable": {
          "default": [],
          "items": {
            "$ref": "#/$defs/UnreadableRecord"
          },
          "title": "Unreadable",
          "type": "array"
        },
        "unverified": {
          "default": [],
          "items": {
            "pattern": "^[0-9a-f]{12}$",
            "type": "string"
          },
          "title": "Unverified",
          "type": "array"
        }
      },
      "required": [
        "instances"
      ],
      "title": "InstanceList",
      "type": "object"
    },
    "InstanceStatus": {
      "description": "How far a live server has come; run status itself is on its socket.",
      "enum": [
        "starting",
        "serving"
      ],
      "title": "InstanceStatus",
      "type": "string"
    },
    "InstanceStopResult": {
      "additionalProperties": false,
      "description": "``vibesys instances stop --json``.",
      "properties": {
        "version": {
          "const": 1,
          "default": 1,
          "title": "Version",
          "type": "integer"
        },
        "id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Id",
          "type": "string"
        },
        "outcome": {
          "$ref": "#/$defs/StopOutcome"
        },
        "route": {
          "anyOf": [
            {
              "$ref": "#/$defs/StopRoute"
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
        "outcome"
      ],
      "title": "InstanceStopResult",
      "type": "object"
    },
    "InstancesChanged": {
      "additionalProperties": false,
      "description": "A push on a watch: the listing as it is now, sent first and then on each change.",
      "properties": {
        "type": {
          "const": "instances.changed",
          "default": "instances.changed",
          "title": "Type",
          "type": "string"
        },
        "watch_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Watch Id",
          "type": "string"
        },
        "listing": {
          "$ref": "#/$defs/InstanceList"
        }
      },
      "required": [
        "type",
        "watch_id",
        "listing"
      ],
      "title": "InstancesChanged",
      "type": "object"
    },
    "InstancesInspectReply": {
      "additionalProperties": false,
      "description": "One coherent inspection of a detached server and its retained artifacts.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "instances.inspect",
          "default": "instances.inspect",
          "title": "Type",
          "type": "string"
        },
        "inspection": {
          "$ref": "#/$defs/InstanceInspection"
        }
      },
      "required": [
        "request_id",
        "type",
        "inspection"
      ],
      "title": "InstancesInspectReply",
      "type": "object"
    },
    "InstancesInspectRequest": {
      "additionalProperties": false,
      "description": "Inspect one detached server without starting another host process.\n\n``include_log`` is deprecated: it asks for a tail of ``server.log``, the detached\nserver's raw output, which is for a person at a terminal. No client UI may depend on\nit; a run's logs come from its log store through ``logs.list`` and ``logs.read``. The\nhelper keeps answering it so older clients keep working, and removes it at the next\nhelper protocol version (2).",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "instances.inspect",
          "default": "instances.inspect",
          "title": "Type",
          "type": "string"
        },
        "instance_id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Instance Id",
          "type": "string"
        },
        "include_log": {
          "default": false,
          "title": "Include Log",
          "type": "boolean"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "instance_id"
      ],
      "title": "InstancesInspectRequest",
      "type": "object"
    },
    "InstancesListReply": {
      "additionalProperties": false,
      "description": "The registry's listing.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "instances.list",
          "default": "instances.list",
          "title": "Type",
          "type": "string"
        },
        "listing": {
          "$ref": "#/$defs/InstanceList"
        }
      },
      "required": [
        "request_id",
        "type",
        "listing"
      ],
      "title": "InstancesListReply",
      "type": "object"
    },
    "InstancesListRequest": {
      "additionalProperties": false,
      "description": "The live and recently ended detached servers on this host.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "instances.list",
          "default": "instances.list",
          "title": "Type",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type"
      ],
      "title": "InstancesListRequest",
      "type": "object"
    },
    "InstancesUnwatchReply": {
      "additionalProperties": false,
      "description": "The watch ended; no ``instances.changed`` for ``watch_id`` follows.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "instances.unwatch",
          "default": "instances.unwatch",
          "title": "Type",
          "type": "string"
        },
        "watch_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Watch Id",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "type",
        "watch_id"
      ],
      "title": "InstancesUnwatchReply",
      "type": "object"
    },
    "InstancesUnwatchRequest": {
      "additionalProperties": false,
      "description": "Stop the watch that ``watch_id`` (its request's id) started.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "instances.unwatch",
          "default": "instances.unwatch",
          "title": "Type",
          "type": "string"
        },
        "watch_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Watch Id",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "watch_id"
      ],
      "title": "InstancesUnwatchRequest",
      "type": "object"
    },
    "InstancesWatchReply": {
      "additionalProperties": false,
      "description": "The watch started; ``instances.changed`` for ``watch_id`` follows at once and on change.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "instances.watch",
          "default": "instances.watch",
          "title": "Type",
          "type": "string"
        },
        "watch_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Watch Id",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "type",
        "watch_id"
      ],
      "title": "InstancesWatchReply",
      "type": "object"
    },
    "InstancesWatchRequest": {
      "additionalProperties": false,
      "description": "Push the listing now and again whenever it changes, until ``instances.unwatch``.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "instances.watch",
          "default": "instances.watch",
          "title": "Type",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type"
      ],
      "title": "InstancesWatchRequest",
      "type": "object"
    },
    "JsonValue": {},
    "LiveInstanceRecord": {
      "additionalProperties": false,
      "description": "What a detached server publishes about itself (version 5).\n\n``run_id`` is allocated by the launcher before the server starts, so the\nrecord names its run from the first time it is published. ``phase`` is the\nrun's lifecycle as the server folds it from its journal, and ``paused`` who\npaused it while it is paused. When the server exits it leaves its last\nrecord, with an ended phase, ``ended_at`` and ``end``, as a tombstone the\nregistry keeps for a day.",
      "properties": {
        "version": {
          "const": 5,
          "default": 5,
          "title": "Version",
          "type": "integer"
        },
        "id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Id",
          "type": "string"
        },
        "status": {
          "$ref": "#/$defs/InstanceStatus"
        },
        "phase": {
          "$ref": "#/$defs/RunPhase"
        },
        "socket_path": {
          "title": "Socket Path",
          "type": "string"
        },
        "log_path": {
          "title": "Log Path",
          "type": "string"
        },
        "project_root": {
          "title": "Project Root",
          "type": "string"
        },
        "run_id": {
          "minLength": 1,
          "title": "Run Id",
          "type": "string"
        },
        "pid": {
          "exclusiveMinimum": 0,
          "title": "Pid",
          "type": "integer"
        },
        "started_at": {
          "maximum": 8640000000000,
          "minimum": 0,
          "title": "Started At",
          "type": "number"
        },
        "ended_at": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Ended At"
        },
        "paused": {
          "anyOf": [
            {
              "$ref": "#/$defs/PauseCause"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "end": {
          "anyOf": [
            {
              "$ref": "#/$defs/EndRecord"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "hostname": {
          "title": "Hostname",
          "type": "string"
        },
        "protocol_version": {
          "const": 1,
          "default": 1,
          "title": "Protocol Version",
          "type": "integer"
        },
        "vibesys_version": {
          "title": "Vibesys Version",
          "type": "string"
        },
        "vibesys_root": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Vibesys Root"
        }
      },
      "required": [
        "id",
        "status",
        "phase",
        "socket_path",
        "log_path",
        "project_root",
        "run_id",
        "pid",
        "started_at",
        "hostname",
        "vibesys_version"
      ],
      "title": "LiveInstanceRecord",
      "type": "object"
    },
    "LogChunk": {
      "additionalProperties": false,
      "description": "Bytes ``[offset, next_offset)`` of a stream, decoded as UTF-8 text.\n\nThe helper cuts a chunk that more bytes follow after the last line end in\nits window, or, with no line end there, before a character the window\nsplits; so ``data`` decodes without loss except for bytes that are not\nUTF-8, which become U+FFFD. ``next_offset - offset`` counts bytes.",
      "properties": {
        "offset": {
          "minimum": 0,
          "title": "Offset",
          "type": "integer"
        },
        "data": {
          "title": "Data",
          "type": "string",
          "x-vibesys-max-utf8-bytes": 196608
        },
        "next_offset": {
          "minimum": 0,
          "title": "Next Offset",
          "type": "integer"
        },
        "eof": {
          "title": "Eof",
          "type": "boolean"
        },
        "closed": {
          "title": "Closed",
          "type": "boolean"
        },
        "truncated_before": {
          "title": "Truncated Before",
          "type": "boolean"
        },
        "pipes": {
          "items": {
            "$ref": "#/$defs/LogPipeSpan"
          },
          "title": "Pipes",
          "type": "array"
        }
      },
      "required": [
        "offset",
        "data",
        "next_offset",
        "eof",
        "closed",
        "truncated_before",
        "pipes"
      ],
      "title": "LogChunk",
      "type": "object"
    },
    "LogOffset": {
      "additionalProperties": false,
      "description": "Read from this byte offset of the stream.",
      "properties": {
        "offset": {
          "minimum": 0,
          "title": "Offset",
          "type": "integer"
        }
      },
      "required": [
        "offset"
      ],
      "title": "LogOffset",
      "type": "object"
    },
    "LogPipe": {
      "description": "Which of a command's output pipes wrote some bytes.",
      "enum": [
        "stdout",
        "stderr"
      ],
      "title": "LogPipe",
      "type": "string"
    },
    "LogPipeSpan": {
      "additionalProperties": false,
      "description": "Bytes ``[offset, offset + length)`` of a stream were written by ``pipe``.",
      "properties": {
        "offset": {
          "minimum": 0,
          "title": "Offset",
          "type": "integer"
        },
        "length": {
          "exclusiveMinimum": 0,
          "title": "Length",
          "type": "integer"
        },
        "pipe": {
          "$ref": "#/$defs/LogPipe"
        }
      },
      "required": [
        "offset",
        "length",
        "pipe"
      ],
      "title": "LogPipeSpan",
      "type": "object"
    },
    "LogStream": {
      "additionalProperties": false,
      "description": "One recorded log stream of an instance, as its store indexes it.",
      "properties": {
        "id": {
          "pattern": "^[0-9]{6,12}$",
          "title": "Id",
          "type": "string"
        },
        "owner": {
          "additionalProperties": {
            "$ref": "#/$defs/JsonValue"
          },
          "title": "Owner",
          "type": "object"
        },
        "command": {
          "title": "Command",
          "type": "string"
        },
        "retained_start": {
          "minimum": 0,
          "title": "Retained Start",
          "type": "integer"
        },
        "size": {
          "minimum": 0,
          "title": "Size",
          "type": "integer"
        },
        "outcome": {
          "anyOf": [
            {
              "$ref": "#/$defs/LogStreamOutcome"
            },
            {
              "type": "null"
            }
          ]
        }
      },
      "required": [
        "id",
        "owner",
        "command",
        "retained_start",
        "size",
        "outcome"
      ],
      "title": "LogStream",
      "type": "object"
    },
    "LogStreamOutcome": {
      "description": "How a stream was closed.",
      "enum": [
        "ended",
        "failed",
        "abandoned"
      ],
      "title": "LogStreamOutcome",
      "type": "string"
    },
    "LogTail": {
      "additionalProperties": false,
      "description": "Read the last ``tail_bytes`` bytes the stream holds, from the first line start in them.",
      "properties": {
        "tail_bytes": {
          "maximum": 65536,
          "minimum": 1,
          "title": "Tail Bytes",
          "type": "integer"
        }
      },
      "required": [
        "tail_bytes"
      ],
      "title": "LogTail",
      "type": "object"
    },
    "LogsCancelReply": {
      "additionalProperties": false,
      "description": "The withdrawal was taken: a read ``read_id`` named that was still waiting stops waiting.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "logs.cancel",
          "default": "logs.cancel",
          "title": "Type",
          "type": "string"
        },
        "read_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Read Id",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "type",
        "read_id"
      ],
      "title": "LogsCancelReply",
      "type": "object"
    },
    "LogsCancelRequest": {
      "additionalProperties": false,
      "description": "Withdraw the ``logs.read`` whose request id is ``read_id``: a wait it is in ends now.\n\nThe read still answers, with what it holds (an empty chunk at the end of the\nstream), and its worker returns, so the channel can admit another request.\nNaming a read that already answered, or no read, is not an error: withdrawal\nraces the reply by nature. Admitted even when the channel's workers are full.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "logs.cancel",
          "default": "logs.cancel",
          "title": "Type",
          "type": "string"
        },
        "read_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Read Id",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "read_id"
      ],
      "title": "LogsCancelRequest",
      "type": "object"
    },
    "LogsListReply": {
      "additionalProperties": false,
      "description": "The instance's streams, oldest first.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "logs.list",
          "default": "logs.list",
          "title": "Type",
          "type": "string"
        },
        "streams": {
          "items": {
            "$ref": "#/$defs/LogStream"
          },
          "title": "Streams",
          "type": "array"
        }
      },
      "required": [
        "request_id",
        "type",
        "streams"
      ],
      "title": "LogsListReply",
      "type": "object"
    },
    "LogsListRequest": {
      "additionalProperties": false,
      "description": "The log streams recorded for one instance, live or ended.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "logs.list",
          "default": "logs.list",
          "title": "Type",
          "type": "string"
        },
        "instance_id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Instance Id",
          "type": "string"
        },
        "span_id": {
          "anyOf": [
            {
              "pattern": "^(flow|op):\\S*$",
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Span Id"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "instance_id"
      ],
      "title": "LogsListRequest",
      "type": "object"
    },
    "LogsReadReply": {
      "additionalProperties": false,
      "description": "One chunk of the stream.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "logs.read",
          "default": "logs.read",
          "title": "Type",
          "type": "string"
        },
        "chunk": {
          "$ref": "#/$defs/LogChunk"
        }
      },
      "required": [
        "request_id",
        "type",
        "chunk"
      ],
      "title": "LogsReadReply",
      "type": "object"
    },
    "LogsReadRequest": {
      "additionalProperties": false,
      "description": "One bounded chunk of a stream; at the end of an open stream, wait up to ``wait_ms`` for more.\n\nA wait ends as soon as bytes arrive or the stream closes, and always before\nthe request's deadline, so a follower keeps exactly one read in flight.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "logs.read",
          "default": "logs.read",
          "title": "Type",
          "type": "string"
        },
        "instance_id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Instance Id",
          "type": "string"
        },
        "stream_id": {
          "pattern": "^[0-9]{6,12}$",
          "title": "Stream Id",
          "type": "string"
        },
        "start": {
          "anyOf": [
            {
              "$ref": "#/$defs/LogOffset"
            },
            {
              "$ref": "#/$defs/LogTail"
            }
          ],
          "title": "Start"
        },
        "max_bytes": {
          "maximum": 65536,
          "minimum": 4,
          "title": "Max Bytes",
          "type": "integer"
        },
        "wait_ms": {
          "default": 0,
          "maximum": 30000,
          "minimum": 0,
          "title": "Wait Ms",
          "type": "integer"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "instance_id",
        "stream_id",
        "start",
        "max_bytes"
      ],
      "title": "LogsReadRequest",
      "type": "object"
    },
    "MachineCheckout": {
      "additionalProperties": false,
      "description": "The VibeSys checkout this helper runs from, which starts runs on this machine.",
      "properties": {
        "root": {
          "title": "Root",
          "type": "string"
        },
        "version": {
          "title": "Version",
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
          "title": "Commit"
        },
        "dirty": {
          "anyOf": [
            {
              "type": "boolean"
            },
            {
              "type": "null"
            }
          ],
          "title": "Dirty"
        }
      },
      "required": [
        "root",
        "version",
        "commit",
        "dirty"
      ],
      "title": "MachineCheckout",
      "type": "object"
    },
    "MachineDescribeReply": {
      "additionalProperties": false,
      "description": "The machine's description.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "machine.describe",
          "default": "machine.describe",
          "title": "Type",
          "type": "string"
        },
        "machine": {
          "$ref": "#/$defs/MachineDescription"
        }
      },
      "required": [
        "request_id",
        "type",
        "machine"
      ],
      "title": "MachineDescribeReply",
      "type": "object"
    },
    "MachineDescribeRequest": {
      "additionalProperties": false,
      "description": "What this host offers runs: its checkout, hardware, Docker, harness CLIs and runs.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "machine.describe",
          "default": "machine.describe",
          "title": "Type",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type"
      ],
      "title": "MachineDescribeRequest",
      "type": "object"
    },
    "MachineDescription": {
      "additionalProperties": false,
      "description": "What one machine offers runs.",
      "properties": {
        "checkout": {
          "anyOf": [
            {
              "$ref": "#/$defs/MachineCheckout"
            },
            {
              "type": "null"
            }
          ]
        },
        "hardware": {
          "$ref": "#/$defs/Hardware"
        },
        "docker": {
          "anyOf": [
            {
              "$ref": "#/$defs/DockerReachable"
            },
            {
              "$ref": "#/$defs/DockerUnreachable"
            }
          ],
          "title": "Docker"
        },
        "harness_clis": {
          "items": {
            "type": "string"
          },
          "title": "Harness Clis",
          "type": "array"
        },
        "runs": {
          "$ref": "#/$defs/RunCounts"
        }
      },
      "required": [
        "checkout",
        "hardware",
        "docker",
        "harness_clis",
        "runs"
      ],
      "title": "MachineDescription",
      "type": "object"
    },
    "MissingLogTail": {
      "additionalProperties": false,
      "description": "The instance has no retained ``server.log`` file.",
      "properties": {
        "state": {
          "const": "missing",
          "default": "missing",
          "title": "State",
          "type": "string"
        }
      },
      "title": "MissingLogTail",
      "type": "object",
      "required": [
        "state"
      ]
    },
    "PathEntry": {
      "additionalProperties": false,
      "description": "One directory that completes the typed path, with what it looks like.",
      "properties": {
        "name": {
          "title": "Name",
          "type": "string"
        },
        "path": {
          "title": "Path",
          "type": "string"
        },
        "vibesys_checkout": {
          "title": "Vibesys Checkout",
          "type": "boolean"
        },
        "vibesys_project": {
          "title": "Vibesys Project",
          "type": "boolean"
        },
        "git_repository": {
          "title": "Git Repository",
          "type": "boolean"
        }
      },
      "required": [
        "name",
        "path",
        "vibesys_checkout",
        "vibesys_project",
        "git_repository"
      ],
      "title": "PathEntry",
      "type": "object"
    },
    "PathsCompleteReply": {
      "additionalProperties": false,
      "description": "Matching directories of ``directory``, sorted by name, at most ``PATHS_COMPLETE_CAP``.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "paths.complete",
          "default": "paths.complete",
          "title": "Type",
          "type": "string"
        },
        "directory": {
          "title": "Directory",
          "type": "string"
        },
        "entries": {
          "items": {
            "$ref": "#/$defs/PathEntry"
          },
          "title": "Entries",
          "type": "array"
        },
        "truncated": {
          "title": "Truncated",
          "type": "boolean"
        }
      },
      "required": [
        "request_id",
        "type",
        "directory",
        "entries",
        "truncated"
      ],
      "title": "PathsCompleteReply",
      "type": "object"
    },
    "PathsCompleteRequest": {
      "additionalProperties": false,
      "description": "The directories that complete a typed path on the host.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "paths.complete",
          "default": "paths.complete",
          "title": "Type",
          "type": "string"
        },
        "prefix": {
          "maxLength": 4096,
          "title": "Prefix",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "prefix"
      ],
      "title": "PathsCompleteRequest",
      "type": "object"
    },
    "PauseCause": {
      "description": "Who paused a paused run.",
      "enum": [
        "user",
        "quota"
      ],
      "title": "PauseCause",
      "type": "string"
    },
    "PingReply": {
      "additionalProperties": false,
      "description": "The channel is alive.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "ping",
          "default": "ping",
          "title": "Type",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "type"
      ],
      "title": "PingReply",
      "type": "object"
    },
    "PingRequest": {
      "additionalProperties": false,
      "description": "Liveness of the channel.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "ping",
          "default": "ping",
          "title": "Type",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type"
      ],
      "title": "PingRequest",
      "type": "object"
    },
    "ResumeLatest": {
      "additionalProperties": false,
      "description": "Resume the project's current or latest run; the launch resolves it before detaching.",
      "properties": {
        "latest": {
          "const": true,
          "title": "Latest",
          "type": "boolean"
        }
      },
      "required": [
        "latest"
      ],
      "title": "ResumeLatest",
      "type": "object"
    },
    "ResumeRun": {
      "additionalProperties": false,
      "description": "Resume this run.",
      "properties": {
        "run_id": {
          "minLength": 1,
          "title": "Run Id",
          "type": "string"
        }
      },
      "required": [
        "run_id"
      ],
      "title": "ResumeRun",
      "type": "object"
    },
    "RunConnectReply": {
      "additionalProperties": false,
      "description": "The channel is now a byte relay carrying the run protocol to this run.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "run.connect",
          "default": "run.connect",
          "title": "Type",
          "type": "string"
        },
        "run_id": {
          "minLength": 1,
          "title": "Run Id",
          "type": "string"
        },
        "protocol_version": {
          "exclusiveMinimum": 0,
          "title": "Protocol Version",
          "type": "integer"
        }
      },
      "required": [
        "request_id",
        "type",
        "run_id",
        "protocol_version"
      ],
      "title": "RunConnectReply",
      "type": "object"
    },
    "RunConnectRequest": {
      "additionalProperties": false,
      "description": "Make this channel a byte relay to a live run's control socket.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "run.connect",
          "default": "run.connect",
          "title": "Type",
          "type": "string"
        },
        "instance_id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Instance Id",
          "type": "string"
        },
        "role": {
          "$ref": "#/$defs/RunRole"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "instance_id",
        "role"
      ],
      "title": "RunConnectRequest",
      "type": "object"
    },
    "RunCounts": {
      "additionalProperties": false,
      "description": "Runs on this machine: live now, and ended since the host's local midnight.",
      "properties": {
        "live": {
          "minimum": 0,
          "title": "Live",
          "type": "integer"
        },
        "ended_today": {
          "minimum": 0,
          "title": "Ended Today",
          "type": "integer"
        }
      },
      "required": [
        "live",
        "ended_today"
      ],
      "title": "RunCounts",
      "type": "object"
    },
    "RunEndOutcome": {
      "description": "Why a run ended, on its tombstone: vs-core's ``EndReason`` members and two of the shell's.\n\nThe first seven are ``vs_core``'s ``EndReason`` values, copied because this module\nmay not import the core (``tests/server/test_run_end_record.py`` keeps them equal).",
      "enum": [
        "adopted",
        "kept-input",
        "no-winner",
        "adoption-failed",
        "stopped",
        "deadline",
        "finish-refused",
        "failed",
        "force_stopped"
      ],
      "title": "RunEndOutcome",
      "type": "string"
    },
    "RunList": {
      "additionalProperties": false,
      "description": "A project's runs, oldest first.",
      "properties": {
        "project_root": {
          "title": "Project Root",
          "type": "string"
        },
        "runs": {
          "items": {
            "$ref": "#/$defs/RunSummary"
          },
          "title": "Runs",
          "type": "array"
        }
      },
      "required": [
        "project_root",
        "runs"
      ],
      "title": "RunList",
      "type": "object"
    },
    "RunPhase": {
      "additionalProperties": false,
      "description": "A run's lifecycle status with the detail its status needs.\n\n``stage`` (and, when the stage reports it, ``progress``) says where a\n``starting`` run is; ``reason`` says why a ``failed`` one ended. None of\nthem is present for any other status.",
      "properties": {
        "status": {
          "$ref": "#/$defs/RunStatus"
        },
        "stage": {
          "anyOf": [
            {
              "$ref": "#/$defs/StartupStage"
            },
            {
              "type": "null"
            }
          ],
          "default": null
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
        "reason": {
          "anyOf": [
            {
              "$ref": "#/$defs/FailureReason"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        }
      },
      "required": [
        "status"
      ],
      "title": "RunPhase",
      "type": "object"
    },
    "RunRole": {
      "description": "Which run-protocol connection a run channel carries (``WP-ROLES``).",
      "enum": [
        "control",
        "subscribe",
        "chat"
      ],
      "title": "RunRole",
      "type": "string"
    },
    "RunStartCompensationEvidence": {
      "discriminator": {
        "mapping": {
          "stop_failed": "#/$defs/StopFailedCompensationFailure",
          "stop_unconfirmed": "#/$defs/StopUnconfirmedCompensationFailure",
          "worker_start_refused": "#/$defs/WorkerStartRefusedCompensationFailure"
        },
        "propertyName": "kind"
      },
      "oneOf": [
        {
          "$ref": "#/$defs/WorkerStartRefusedCompensationFailure"
        },
        {
          "$ref": "#/$defs/StopFailedCompensationFailure"
        },
        {
          "$ref": "#/$defs/StopUnconfirmedCompensationFailure"
        }
      ]
    },
    "RunStartCompensationFailure": {
      "$ref": "#/$defs/RunStartCompensationEvidence",
      "description": "One strict stderr line preserving an undisclosed instance cleanup failure.\n\nA successful ``run.start`` reply could not reach the client, so only this\ndiagnostic can tell an operator which live instance may need recovery. The\ndiscriminated variants make result presence and nonterminal outcomes part of\nthe generated schema rather than Python-only validation.",
      "title": "RunStartCompensationFailure"
    },
    "RunStartReply": {
      "additionalProperties": false,
      "description": "The started server's record, or why nothing started.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "run.start",
          "default": "run.start",
          "title": "Type",
          "type": "string"
        },
        "launch": {
          "anyOf": [
            {
              "$ref": "#/$defs/LiveInstanceRecord"
            },
            {
              "$ref": "#/$defs/DetachedLaunchFailure"
            }
          ],
          "title": "Launch"
        }
      },
      "required": [
        "request_id",
        "type",
        "launch"
      ],
      "title": "RunStartReply",
      "type": "object"
    },
    "RunStartRequest": {
      "additionalProperties": false,
      "description": "Start a detached run (``vibesys --detach``) in ``project``.\n\n``args`` never carry ``UNVETTED_MODEL_FLAG`` (or an abbreviation of it): a run\nstarted through the helper uses only vetted harnesses. With ``harness``, they\ncarry neither ``CLI_PROVIDER_FLAG`` nor ``MODEL_FLAG``, so the pair has one source.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "run.start",
          "default": "run.start",
          "title": "Type",
          "type": "string"
        },
        "project": {
          "title": "Project",
          "type": "string"
        },
        "task": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Task"
        },
        "args": {
          "default": [],
          "items": {
            "type": "string"
          },
          "title": "Args",
          "type": "array"
        },
        "resume": {
          "anyOf": [
            {
              "$ref": "#/$defs/ResumeRun"
            },
            {
              "$ref": "#/$defs/ResumeLatest"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Resume"
        },
        "harness": {
          "anyOf": [
            {
              "$ref": "#/$defs/HarnessChoice"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "project"
      ],
      "title": "RunStartRequest",
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
    "RunStopReply": {
      "additionalProperties": false,
      "description": "What the stop observed.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "run.stop",
          "default": "run.stop",
          "title": "Type",
          "type": "string"
        },
        "result": {
          "$ref": "#/$defs/InstanceStopResult"
        }
      },
      "required": [
        "request_id",
        "type",
        "result"
      ],
      "title": "RunStopReply",
      "type": "object"
    },
    "RunStopRequest": {
      "additionalProperties": false,
      "description": "Stop a live detached server (``vibesys instances stop``).",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "run.stop",
          "default": "run.stop",
          "title": "Type",
          "type": "string"
        },
        "instance_id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Instance Id",
          "type": "string"
        },
        "force": {
          "default": false,
          "title": "Force",
          "type": "boolean"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "instance_id"
      ],
      "title": "RunStopRequest",
      "type": "object"
    },
    "RunSummary": {
      "additionalProperties": false,
      "description": "One run of a project, from its durable manifest and this host's registry.",
      "properties": {
        "run_id": {
          "minLength": 1,
          "title": "Run Id",
          "type": "string"
        },
        "task": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "title": "Task"
        },
        "display_name": {
          "title": "Display Name",
          "type": "string"
        },
        "created_at": {
          "format": "date-time",
          "title": "Created At",
          "type": "string"
        },
        "phase": {
          "anyOf": [
            {
              "$ref": "#/$defs/RunPhase"
            },
            {
              "type": "null"
            }
          ]
        },
        "instance_id": {
          "anyOf": [
            {
              "pattern": "^[0-9a-f]{12}$",
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "title": "Instance Id"
        }
      },
      "required": [
        "run_id",
        "task",
        "display_name",
        "created_at",
        "phase",
        "instance_id"
      ],
      "title": "RunSummary",
      "type": "object"
    },
    "RunsListReply": {
      "additionalProperties": false,
      "description": "The project's runs.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "runs.list",
          "default": "runs.list",
          "title": "Type",
          "type": "string"
        },
        "runs": {
          "$ref": "#/$defs/RunList"
        }
      },
      "required": [
        "request_id",
        "type",
        "runs"
      ],
      "title": "RunsListReply",
      "type": "object"
    },
    "RunsListRequest": {
      "additionalProperties": false,
      "description": "A project's runs, with the phase of any server on this host that drives one.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "runs.list",
          "default": "runs.list",
          "title": "Type",
          "type": "string"
        },
        "project": {
          "title": "Project",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "project"
      ],
      "title": "RunsListRequest",
      "type": "object"
    },
    "RunsReadEventsReply": {
      "additionalProperties": false,
      "description": "One page of a run's recorded events, oldest first.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "runs.read_events",
          "default": "runs.read_events",
          "title": "Type",
          "type": "string"
        },
        "events": {
          "items": {
            "additionalProperties": {
              "$ref": "#/$defs/JsonValue"
            },
            "type": "object"
          },
          "title": "Events",
          "type": "array"
        },
        "next_sequence": {
          "exclusiveMinimum": 0,
          "title": "Next Sequence",
          "type": "integer"
        },
        "complete": {
          "title": "Complete",
          "type": "boolean"
        }
      },
      "required": [
        "request_id",
        "type",
        "events",
        "next_sequence",
        "complete"
      ],
      "title": "RunsReadEventsReply",
      "type": "object"
    },
    "RunsReadEventsRequest": {
      "additionalProperties": false,
      "description": "A page of one run's recorded events, from its journal on disk, live or ended.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "runs.read_events",
          "default": "runs.read_events",
          "title": "Type",
          "type": "string"
        },
        "project": {
          "title": "Project",
          "type": "string"
        },
        "run_id": {
          "minLength": 1,
          "title": "Run Id",
          "type": "string"
        },
        "from_sequence": {
          "exclusiveMinimum": 0,
          "title": "From Sequence",
          "type": "integer"
        },
        "max": {
          "maximum": 500,
          "minimum": 1,
          "title": "Max",
          "type": "integer"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "project",
        "run_id",
        "from_sequence",
        "max"
      ],
      "title": "RunsReadEventsRequest",
      "type": "object"
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
        "building_image",
        "starting_container"
      ],
      "title": "StartupStage",
      "type": "string"
    },
    "StopFailedCompensationFailure": {
      "additionalProperties": false,
      "description": "The registry stop raised before returning typed evidence.",
      "properties": {
        "type": {
          "const": "run.start_compensation_failed",
          "title": "Type",
          "type": "string"
        },
        "exit_status": {
          "const": 12,
          "title": "Exit Status",
          "type": "integer"
        },
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "detail": {
          "maxLength": 2000,
          "minLength": 1,
          "title": "Detail",
          "type": "string"
        },
        "kind": {
          "const": "stop_failed",
          "title": "Kind",
          "type": "string"
        },
        "instance_id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Instance Id",
          "type": "string"
        }
      },
      "required": [
        "type",
        "exit_status",
        "request_id",
        "detail",
        "kind",
        "instance_id"
      ],
      "title": "StopFailedCompensationFailure",
      "type": "object"
    },
    "StopOutcome": {
      "description": "What ``vibesys instances stop`` observed.",
      "enum": [
        "stopped",
        "stopping",
        "not_running",
        "still_running",
        "unsupported"
      ],
      "title": "StopOutcome",
      "type": "string"
    },
    "StopRoute": {
      "description": "How ``vibesys instances stop`` reached the server.",
      "enum": [
        "control_socket",
        "signal"
      ],
      "title": "StopRoute",
      "type": "string"
    },
    "StopUnconfirmedCompensationFailure": {
      "additionalProperties": false,
      "description": "The registry returned a typed result that does not prove terminal cleanup.\n\nThe stop result is the one source of the instance identity in this variant,\nso a serialized report cannot carry two identities that disagree.",
      "properties": {
        "type": {
          "const": "run.start_compensation_failed",
          "title": "Type",
          "type": "string"
        },
        "exit_status": {
          "const": 12,
          "title": "Exit Status",
          "type": "integer"
        },
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "detail": {
          "maxLength": 2000,
          "minLength": 1,
          "title": "Detail",
          "type": "string"
        },
        "kind": {
          "const": "stop_unconfirmed",
          "title": "Kind",
          "type": "string"
        },
        "stop_result": {
          "$ref": "#/$defs/UnconfirmedInstanceStopResult"
        }
      },
      "required": [
        "type",
        "exit_status",
        "request_id",
        "detail",
        "kind",
        "stop_result"
      ],
      "title": "StopUnconfirmedCompensationFailure",
      "type": "object"
    },
    "TaskList": {
      "additionalProperties": false,
      "description": "``vibesys tasks --json``: the tasks of one project.",
      "properties": {
        "version": {
          "const": 1,
          "default": 1,
          "title": "Version",
          "type": "integer"
        },
        "project_root": {
          "title": "Project Root",
          "type": "string"
        },
        "tasks": {
          "items": {
            "$ref": "#/$defs/TaskSummary"
          },
          "title": "Tasks",
          "type": "array"
        }
      },
      "required": [
        "project_root",
        "tasks"
      ],
      "title": "TaskList",
      "type": "object"
    },
    "TaskSummary": {
      "additionalProperties": false,
      "description": "One task of a project.",
      "properties": {
        "name": {
          "title": "Name",
          "type": "string"
        }
      },
      "required": [
        "name"
      ],
      "title": "TaskSummary",
      "type": "object"
    },
    "TasksListReply": {
      "additionalProperties": false,
      "description": "The project's tasks.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "type": {
          "const": "tasks.list",
          "default": "tasks.list",
          "title": "Type",
          "type": "string"
        },
        "tasks": {
          "$ref": "#/$defs/TaskList"
        }
      },
      "required": [
        "request_id",
        "type",
        "tasks"
      ],
      "title": "TasksListReply",
      "type": "object"
    },
    "TasksListRequest": {
      "additionalProperties": false,
      "description": "The tasks a project defines under ``.vibesys/tasks/``.",
      "properties": {
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "deadline_ms": {
          "exclusiveMinimum": 0,
          "maximum": 3600000,
          "title": "Deadline Ms",
          "type": "integer"
        },
        "type": {
          "const": "tasks.list",
          "default": "tasks.list",
          "title": "Type",
          "type": "string"
        },
        "project": {
          "title": "Project",
          "type": "string"
        }
      },
      "required": [
        "request_id",
        "deadline_ms",
        "type",
        "project"
      ],
      "title": "TasksListRequest",
      "type": "object"
    },
    "UnconfirmedInstanceStopResult": {
      "additionalProperties": false,
      "description": "A complete stop result whose outcome does not prove terminal cleanup.",
      "properties": {
        "version": {
          "const": 1,
          "default": 1,
          "title": "Version",
          "type": "integer"
        },
        "id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Id",
          "type": "string"
        },
        "outcome": {
          "enum": [
            "stopping",
            "still_running",
            "unsupported"
          ],
          "title": "Outcome",
          "type": "string"
        },
        "route": {
          "anyOf": [
            {
              "$ref": "#/$defs/StopRoute"
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
        "outcome"
      ],
      "title": "UnconfirmedInstanceStopResult",
      "type": "object"
    },
    "UnreadableRecord": {
      "additionalProperties": false,
      "description": "A registry entry this reader cannot interpret; ``version`` is ``None`` if it has none.",
      "properties": {
        "id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Id",
          "type": "string"
        },
        "version": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Version"
        }
      },
      "required": [
        "id",
        "version"
      ],
      "title": "UnreadableRecord",
      "type": "object"
    },
    "WorkerStartRefusedCompensationFailure": {
      "additionalProperties": false,
      "description": "The helper could not start the worker that owns the compensating stop.",
      "properties": {
        "type": {
          "const": "run.start_compensation_failed",
          "title": "Type",
          "type": "string"
        },
        "exit_status": {
          "const": 12,
          "title": "Exit Status",
          "type": "integer"
        },
        "request_id": {
          "maxLength": 128,
          "minLength": 1,
          "title": "Request Id",
          "type": "string"
        },
        "detail": {
          "maxLength": 2000,
          "minLength": 1,
          "title": "Detail",
          "type": "string"
        },
        "kind": {
          "const": "worker_start_refused",
          "title": "Kind",
          "type": "string"
        },
        "instance_id": {
          "pattern": "^[0-9a-f]{12}$",
          "title": "Instance Id",
          "type": "string"
        }
      },
      "required": [
        "type",
        "exit_status",
        "request_id",
        "detail",
        "kind",
        "instance_id"
      ],
      "title": "WorkerStartRefusedCompensationFailure",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "description": "Root schema document for the host helper: what a client sends and what it receives.",
  "properties": {
    "request": {
      "discriminator": {
        "mapping": {
          "checkout.inspect": "#/$defs/CheckoutInspectRequest",
          "harnesses.list": "#/$defs/HarnessesListRequest",
          "hello": "#/$defs/HelloRequest",
          "instances.inspect": "#/$defs/InstancesInspectRequest",
          "instances.list": "#/$defs/InstancesListRequest",
          "instances.unwatch": "#/$defs/InstancesUnwatchRequest",
          "instances.watch": "#/$defs/InstancesWatchRequest",
          "logs.cancel": "#/$defs/LogsCancelRequest",
          "logs.list": "#/$defs/LogsListRequest",
          "logs.read": "#/$defs/LogsReadRequest",
          "machine.describe": "#/$defs/MachineDescribeRequest",
          "paths.complete": "#/$defs/PathsCompleteRequest",
          "ping": "#/$defs/PingRequest",
          "run.connect": "#/$defs/RunConnectRequest",
          "run.start": "#/$defs/RunStartRequest",
          "run.stop": "#/$defs/RunStopRequest",
          "runs.list": "#/$defs/RunsListRequest",
          "runs.read_events": "#/$defs/RunsReadEventsRequest",
          "tasks.list": "#/$defs/TasksListRequest"
        },
        "propertyName": "type"
      },
      "oneOf": [
        {
          "$ref": "#/$defs/HelloRequest"
        },
        {
          "$ref": "#/$defs/InstancesListRequest"
        },
        {
          "$ref": "#/$defs/InstancesInspectRequest"
        },
        {
          "$ref": "#/$defs/InstancesWatchRequest"
        },
        {
          "$ref": "#/$defs/InstancesUnwatchRequest"
        },
        {
          "$ref": "#/$defs/TasksListRequest"
        },
        {
          "$ref": "#/$defs/RunsListRequest"
        },
        {
          "$ref": "#/$defs/RunStartRequest"
        },
        {
          "$ref": "#/$defs/RunStopRequest"
        },
        {
          "$ref": "#/$defs/RunConnectRequest"
        },
        {
          "$ref": "#/$defs/PingRequest"
        },
        {
          "$ref": "#/$defs/PathsCompleteRequest"
        },
        {
          "$ref": "#/$defs/LogsListRequest"
        },
        {
          "$ref": "#/$defs/LogsReadRequest"
        },
        {
          "$ref": "#/$defs/LogsCancelRequest"
        },
        {
          "$ref": "#/$defs/HarnessesListRequest"
        },
        {
          "$ref": "#/$defs/RunsReadEventsRequest"
        },
        {
          "$ref": "#/$defs/MachineDescribeRequest"
        },
        {
          "$ref": "#/$defs/CheckoutInspectRequest"
        }
      ],
      "title": "Request"
    },
    "message": {
      "discriminator": {
        "mapping": {
          "checkout.inspect": "#/$defs/CheckoutInspectReply",
          "error": "#/$defs/HelperError",
          "harnesses.list": "#/$defs/HarnessesListReply",
          "hello": "#/$defs/HelloReply",
          "instances.changed": "#/$defs/InstancesChanged",
          "instances.inspect": "#/$defs/InstancesInspectReply",
          "instances.list": "#/$defs/InstancesListReply",
          "instances.unwatch": "#/$defs/InstancesUnwatchReply",
          "instances.watch": "#/$defs/InstancesWatchReply",
          "logs.cancel": "#/$defs/LogsCancelReply",
          "logs.list": "#/$defs/LogsListReply",
          "logs.read": "#/$defs/LogsReadReply",
          "machine.describe": "#/$defs/MachineDescribeReply",
          "paths.complete": "#/$defs/PathsCompleteReply",
          "ping": "#/$defs/PingReply",
          "run.connect": "#/$defs/RunConnectReply",
          "run.start": "#/$defs/RunStartReply",
          "run.stop": "#/$defs/RunStopReply",
          "runs.list": "#/$defs/RunsListReply",
          "runs.read_events": "#/$defs/RunsReadEventsReply",
          "tasks.list": "#/$defs/TasksListReply"
        },
        "propertyName": "type"
      },
      "oneOf": [
        {
          "$ref": "#/$defs/HelloReply"
        },
        {
          "$ref": "#/$defs/InstancesListReply"
        },
        {
          "$ref": "#/$defs/InstancesInspectReply"
        },
        {
          "$ref": "#/$defs/InstancesWatchReply"
        },
        {
          "$ref": "#/$defs/InstancesUnwatchReply"
        },
        {
          "$ref": "#/$defs/TasksListReply"
        },
        {
          "$ref": "#/$defs/RunsListReply"
        },
        {
          "$ref": "#/$defs/RunStartReply"
        },
        {
          "$ref": "#/$defs/RunStopReply"
        },
        {
          "$ref": "#/$defs/RunConnectReply"
        },
        {
          "$ref": "#/$defs/PingReply"
        },
        {
          "$ref": "#/$defs/PathsCompleteReply"
        },
        {
          "$ref": "#/$defs/LogsListReply"
        },
        {
          "$ref": "#/$defs/LogsReadReply"
        },
        {
          "$ref": "#/$defs/LogsCancelReply"
        },
        {
          "$ref": "#/$defs/HarnessesListReply"
        },
        {
          "$ref": "#/$defs/RunsReadEventsReply"
        },
        {
          "$ref": "#/$defs/MachineDescribeReply"
        },
        {
          "$ref": "#/$defs/CheckoutInspectReply"
        },
        {
          "$ref": "#/$defs/InstancesChanged"
        },
        {
          "$ref": "#/$defs/HelperError"
        }
      ],
      "title": "Message"
    },
    "channel_end": {
      "$ref": "#/$defs/ChannelEnd"
    },
    "compensation_failure": {
      "$ref": "#/$defs/RunStartCompensationFailure"
    }
  },
  "required": [
    "request",
    "message",
    "channel_end",
    "compensation_failure"
  ],
  "title": "HostHelperDocument",
  "type": "object",
  "x-vibesys-feature-by-request": {
    "instances.inspect": "instances_inspect",
    "instances.watch": "instances_watch",
    "instances.unwatch": "instances_watch",
    "paths.complete": "paths_complete",
    "logs.list": "logs",
    "logs.read": "logs",
    "logs.cancel": "logs_cancel",
    "harnesses.list": "harnesses",
    "runs.read_events": "run_history",
    "machine.describe": "machine_describe",
    "checkout.inspect": "checkout_inspect"
  },
  "x-vibesys-feature-by-field": {
    "run.start": {
      "harness": "harnesses"
    },
    "logs.list": {
      "span_id": "logs_by_span"
    }
  }
};

export default hostHelperSchema;
