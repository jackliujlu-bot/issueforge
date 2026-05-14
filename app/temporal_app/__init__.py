"""Temporal durable runtime.

Submodules are imported on demand. Keeping the package ``__init__`` empty
matters for Temporal's workflow sandbox: when the sandbox validates
:class:`IssueAgentWorkflow`, it imports
``app.temporal_app.workflows``, which first executes this file. Pulling in
``activities`` here would drag the entire GitHub / executor / rich stack
into the sandbox and trip restricted-operation checks (``random.getrandbits``,
``pathlib.Path.resolve``, etc.).
"""
