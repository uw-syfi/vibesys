"""The errors a Slurm job runner raises: validation, transport, scheduler, and collection."""

from __future__ import annotations


class SlurmError(RuntimeError):
    """Actionable failure from validation, transport, Slurm, or artifact collection."""

    @classmethod
    def invalid_operation_record(cls, operation_id: str) -> SlurmError:
        """Describe a persisted record stored under another operation identity."""
        return cls(f"operation record identity mismatch at {operation_id}.json")

    @classmethod
    def invalid_operation_id(cls) -> SlurmError:
        """Describe an unsafe stable operation identifier."""
        return cls("operation_id must contain 1 to 128 safe identifier characters")

    @classmethod
    def invalid_job_id(cls) -> SlurmError:
        """Describe a scheduler identifier outside the bounded decimal contract."""
        return cls("job_id must contain 1 to 32 decimal digits")

    @classmethod
    def operation_identity_ambiguous(cls) -> SlurmError:
        """Describe multiple scheduler jobs matching a stable operation ID."""
        return cls("stable operation ID matched multiple scheduler jobs")

    @classmethod
    def invalid_script_states(cls) -> SlurmError:
        """Describe an empty or incorrectly typed fake scheduler sequence."""
        return cls("states must contain SlurmJobStatus observations")

    @classmethod
    def invalid_command(cls) -> SlurmError:
        """Describe an empty or malformed command vector."""
        return cls("Slurm job command must be a non-empty argv")

    @classmethod
    def invalid_batch_request(cls) -> SlurmError:
        """Describe an empty batch or an unsafe or repeated stage name."""
        return cls("Slurm batch must contain stages with unique safe names")

    @classmethod
    def invalid_stage_timeout(cls) -> SlurmError:
        """Describe a stage timeout outside the supported integer range."""
        return cls("Slurm batch stage timeout must be a positive integer number of seconds")

    @classmethod
    def content_stage_failed(cls) -> SlurmError:
        """Describe a local tree that could not be safely content-addressed."""
        return cls("Slurm input changed or became invalid during content-addressed staging")

    @classmethod
    def content_cache_busy(cls, detail: str) -> SlurmError:
        """Describe a transient staging condition that can be retried."""
        return cls(f"Slurm content staging is temporarily unavailable: {detail}")

    @classmethod
    def workspace_is_not_directory(cls) -> SlurmError:
        """Describe a missing or non-directory workspace."""
        return cls("Slurm job workspace must be an existing directory")

    @classmethod
    def missing_job_id(cls) -> SlurmError:
        """Describe submission output without a job identifier."""
        return cls("sbatch did not return a Slurm job id")

    @classmethod
    def truncated_output(cls) -> SlurmError:
        """Describe a scheduler answer that lost its end line in transit."""
        return cls("Slurm scheduler output did not arrive whole")

    @classmethod
    def job_timed_out(cls, job_id: str) -> SlurmError:
        """Describe a job that exceeded its configured deadline."""
        return cls(f"Slurm job {job_id} exceeded the configured timeout")

    @classmethod
    def transport_unavailable(cls, operation: str) -> SlurmError:
        """Describe a missing transport executable."""
        return cls(f"Slurm transport is unavailable during {operation}")

    @classmethod
    def transport_timed_out(cls, operation: str) -> SlurmError:
        """Describe a transport request that exceeded its deadline."""
        return cls(f"Slurm transport timed out during {operation}")

    @classmethod
    def transport_failed(cls, operation: str, returncode: int) -> SlurmError:
        """Describe an unsuccessful transport operation without exposing output."""
        return cls(f"Slurm transport {operation} failed with exit code {returncode}")

    @classmethod
    def invalid_connector_response(cls, operation: str) -> SlurmError:
        """Describe a response that violates the connector protocol."""
        return cls(f"Slurm connector returned an invalid response during {operation}")

    @classmethod
    def invalid_artifact_path(cls) -> SlurmError:
        """Describe an artifact outside the staged workspace."""
        return cls("Slurm artifact paths must be safe candidate-relative paths")

    @classmethod
    def invalid_support_path(cls) -> SlurmError:
        """Describe an invalid local or remote support tree."""
        return cls("Slurm support trees must map safe relative paths to directories")

    @classmethod
    def invalid_setup_script(cls) -> SlurmError:
        """Describe an unsafe remote setup script path."""
        return cls("Slurm setup script must be an absolute normalized POSIX path")

    @classmethod
    def remote_path_absent(cls, remote: str) -> SlurmError:
        """Describe a result path the cluster answered, whole, does not exist."""
        return cls(f"{remote} does not exist on the cluster")

    @classmethod
    def malformed_result(cls) -> SlurmError:
        """Describe missing or malformed job result files."""
        return cls("Slurm job returned a missing or malformed status artifact")

    @classmethod
    def cancelled(cls, job_id: str) -> SlurmError:
        """Describe a submitted job cancelled by its caller."""
        return cls(f"Slurm job {job_id} was cancelled")

    @classmethod
    def cancelled_before_submission(cls) -> SlurmError:
        """Describe a request cancelled before side effects began."""
        return cls("Slurm job was cancelled before submission")

    @classmethod
    def job_not_terminal(cls, job_id: str) -> SlurmError:
        """Describe a collection attempt before scheduler completion."""
        return cls(f"Slurm job {job_id} is not terminal")

    @classmethod
    def invalid_wait_timeout(cls) -> SlurmError:
        """Describe a non-positive bounded observation interval."""
        return cls("timeout_seconds must be greater than zero")

    @classmethod
    def handle_config_mismatch(cls) -> SlurmError:
        """Describe a handle used against a different configured cluster."""
        return cls("Slurm job handle does not belong to this cluster configuration")

    @classmethod
    def invalid_handle(cls) -> SlurmError:
        """Describe a handle whose remote paths do not match its identity."""
        return cls("Slurm job handle contains paths outside its operation workspace")

    @classmethod
    def operation_cleanup_unconfirmed(cls) -> SlurmError:
        """Describe operation-owned remote paths that remain after cleanup."""
        return cls("Slurm operation cleanup could not be confirmed")


class SlurmTransferInterruptedError(SlurmError):
    """A download from the cluster failed, and the cluster did not prove its source absent.

    The local copy may be partial (cut before any file, mid-tree, or after the
    last file but before the transfer reported success), so nothing it left
    is evidence. Collection that meets it reads no result at all: the remote
    files may be whole, and a later collection reads them again.
    """

    @classmethod
    def of(cls, remote: str, cause: BaseException) -> SlurmTransferInterruptedError:
        """Name the remote path whose download did not complete, and why."""
        return cls(f"download of {remote} did not complete: {cause}")


class SlurmSubmissionRejectedError(SlurmError):
    """Submission failed before the scheduler submission call was invoked.

    The original SlurmError diagnostic is preserved. Once the sbatch call
    begins, transport loss has unknown acceptance and never uses this type.
    """


class SlurmWorkspaceRootProbeError(SlurmError):
    """The startup probe could not prove the configured workspace root shared."""

    @classmethod
    def job_failed(cls, job_id: str, exit_code: int | None) -> SlurmWorkspaceRootProbeError:
        """Describe a probe job that reached a nonzero or missing exit status."""
        return cls(
            "Slurm `remote_workspace_root` startup probe failed: "
            f"probe job {job_id} exited with code {exit_code}"
        )

    @classmethod
    def node_token_missing(cls, job_id: str) -> SlurmWorkspaceRootProbeError:
        """Describe a node token the submit side could not read or collect."""
        return cls(
            "Slurm `remote_workspace_root` startup probe failed: "
            f"probe job {job_id} returned no collectable node token"
        )

    @classmethod
    def node_token_wrong(cls, job_id: str) -> SlurmWorkspaceRootProbeError:
        """Describe a node token that did not belong to this probe."""
        return cls(
            "Slurm `remote_workspace_root` startup probe failed: "
            f"probe job {job_id} returned the wrong node token"
        )

    @classmethod
    def infrastructure(cls) -> SlurmWorkspaceRootProbeError:
        """Describe an infrastructure failure without exposing boundary diagnostics."""
        return cls("Slurm `remote_workspace_root` startup probe failed: infrastructure error")

    @classmethod
    def submission_unresolved(cls) -> SlurmWorkspaceRootProbeError:
        """Describe scheduler acceptance that could not be reconciled yet."""
        return cls(
            "Slurm `remote_workspace_root` startup probe failed: "
            "probe submission could not be reconciled"
        )

    @classmethod
    def settlement_unconfirmed(cls) -> SlurmWorkspaceRootProbeError:
        """Describe a probe job whose terminal state could not be confirmed."""
        return cls(
            "Slurm `remote_workspace_root` startup probe failed: "
            "probe job settlement could not be confirmed"
        )

    @classmethod
    def cleanup_unconfirmed(cls) -> SlurmWorkspaceRootProbeError:
        """Describe probe-owned remote material that could not be confirmed absent."""
        return cls(
            "Slurm `remote_workspace_root` startup probe failed: "
            "probe cleanup could not be confirmed"
        )

    @classmethod
    def state_invalid(cls) -> SlurmWorkspaceRootProbeError:
        """Describe unreadable durable probe completion evidence."""
        return cls(
            "Slurm `remote_workspace_root` startup probe failed: probe recovery state is invalid"
        )
