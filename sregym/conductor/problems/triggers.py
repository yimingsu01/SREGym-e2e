"""Fault triggers for ``CodeBugProblem``.

A trigger is a callable ``(app) -> None``. ``CodeBugProblem.inject_fault``
calls ``self.trigger(self.app)``. Picking a trigger is per-problem:

- ``NoopTrigger`` — the bug manifests from internal reads during startup
  (e.g. CassandraOomRead). Nothing external needed.
- ``CqlTrigger("SELECT ...")`` — run CQL against a Cassandra-like app.
- ``ShellTrigger("kafka-topics.sh --list ...")`` — run a shell command via the
  app's ``run_admin_command`` hook.
"""

import logging

logger = logging.getLogger(__name__)


class NoopTrigger:
    def __call__(self, app) -> None:
        logger.info("[Trigger] No-op — bug manifests from internal activity")


class CqlTrigger:
    """Execute CQL against an app exposing ``run_cql(cql: str) -> str``."""

    def __init__(self, cql: str):
        self.cql = cql

    def __call__(self, app) -> None:
        logger.info("[Trigger] Executing CQL trigger")
        try:
            out = app.run_cql(self.cql)
            logger.info(f"[Trigger] CQL output: {out!r}")
        except Exception as e:
            # A server-side error IS often the bug manifesting; don't swallow silently.
            logger.info(f"[Trigger] CQL produced error (may be expected): {e}")


class ShellTrigger:
    """Execute an arbitrary shell command via the app's ``run_admin_command`` hook."""

    def __init__(self, command: str):
        self.command = command

    def __call__(self, app) -> None:
        logger.info(f"[Trigger] Running admin command: {self.command}")
        try:
            out = app.run_admin_command(self.command)
            logger.info(f"[Trigger] admin output: {out!r}")
        except Exception as e:
            logger.info(f"[Trigger] admin command produced error (may be expected): {e}")
