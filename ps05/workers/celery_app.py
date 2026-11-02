"""Celery application for PS-05 workers.

Start a worker with::

    celery -A ps05.workers.celery_app worker --loglevel=info

Broker/result backend come from ``ps05.settings`` (``REDIS_URL`` by default).
Task semantics are tuned to the TRD § 23.6 lease/recovery model: at-least-once
delivery with late acks and a prefetch of 1, so a claim is only acknowledged
after the task's unit of work commits.
"""

from celery import Celery

from ps05.settings import get_settings

settings = get_settings()

celery = Celery(
    "ps05",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery.conf.update(
    # At-least-once delivery: ack only after the task's unit of work commits.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_retry_delay=30,
    # § 23.10: bounded retry budgets per task; per-workload max_attempts live
    # in task records, not a single global max_retries.
    broker_connection_retry=True,
    broker_connection_max_retries=100,
    task_serializer="json",
    result_serializer="json",
    accept=["json"],
    timezone="UTC",
)

celery.conf.update(worker_concurrency=settings.celery_concurrency)
