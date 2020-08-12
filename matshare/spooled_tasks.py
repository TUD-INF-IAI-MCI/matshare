import functools
import logging

import uwsgi_tasks


LOGGER = logging.getLogger(__name__)


def spooled_task(func=None, **kwargs):
    """Custom decorator for creating uWSGI tasks running on the spooler.

    When this decorator is used instead of ``uwsgi_tasks.task``, exceptions raised
    during task execution are logged and cause ``uwsgi_tasks.SPOOL_RETRY`` to be
    returned. Use the regular ``retry_*`` parameters to control the maximum number
    of retries and interval. ``spooler_return=False`` is always passed to
    ``uwsgi_tasks.task`` and doesn't need to be specified explicitly.
    """
    if func is None:
        return functools.partial(spooled_task, **kwargs)

    @uwsgi_tasks.task(
        **kwargs, executor=uwsgi_tasks.TaskExecutor.SPOOLER, spooler_return=False
    )
    @functools.wraps(func)
    def _task_wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
        except Exception:
            LOGGER.exception("Exception in task %r", func)
            return uwsgi_tasks.SPOOL_RETRY
        # When task returns no valid SPOOL_* constant, we assume it succeeded
        if result in (uwsgi_tasks.SPOOL_IGNORE, uwsgi_tasks.SPOOL_RETRY):
            return result
        return uwsgi_tasks.SPOOL_OK

    return _task_wrapper
