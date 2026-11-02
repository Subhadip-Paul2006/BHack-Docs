"""PS-05 core orchestration framework (TRD § 23).

Owns analysis-run planning, task state machines, workflow DAGs,
idempotency, outbox/inbox, leases and recovery, checkpoints, the
dead-letter queue, admission control and the workflow-engine facade.

Pure logic: importable with no live service and no Celery dependency.
"""

__version__ = "0.1.0"
