"""Reference implementation of the log-master Logging Server API.

A Python logging server implements a small :class:`~logserver.router.Backend` and gets:

* :mod:`logserver.models` — pydantic models of every request/response (the API schema),
* :func:`~logserver.router.create_app` / :func:`~logserver.router.create_router` — all `/v1` endpoints,
* :class:`~logserver.session.LogSession` — log directories, manifests, segments and start/stop gating,
* :class:`~logserver.config.ConfigStore` — `config.yaml` persistence and JSON Schema generation,
* :func:`~logserver.runner.run_in_thread` — uvicorn beside an existing single-threaded I/O loop.
"""

__version__ = '0.1.0'
