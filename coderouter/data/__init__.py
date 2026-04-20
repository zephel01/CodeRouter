"""Package-data resources for CodeRouter.

Currently holds the bundled ``model-capabilities.yaml`` registry (v0.7-A).
Kept as a real package (with ``__init__.py``) rather than a plain data
directory so that ``importlib.resources.files('coderouter.data')`` works
cleanly under every install mode (wheel / editable / zipapp).
"""
