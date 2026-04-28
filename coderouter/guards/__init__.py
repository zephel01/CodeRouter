"""Long-run reliability guards (v1.9-E).

CodeRouter's third pillar (``docs/inside/future.md`` §1: P3 Long-run
Reliability) lives here. Each module addresses one of the systematic
failure modes that a continuously-running local-LLM agent loop tends
to hit:

  * :mod:`coderouter.guards.tool_loop`      — L3 stuck-tool detection
  * :mod:`coderouter.guards.memory_pressure` — L2 backend OOM
                                                 awareness (planned)
  * :mod:`coderouter.guards.backend_health`  — L5 continuous probe +
                                                 chain reorder (planned)

Each guard is a pure-functional / single-class module that the engine
consults at the appropriate dispatch point. Guards never block the
fast path — they observe and either log, mutate, or short-circuit
based on operator-configured policy.
"""
