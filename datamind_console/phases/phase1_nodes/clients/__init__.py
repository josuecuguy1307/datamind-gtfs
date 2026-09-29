"""
Phase 1 client submodules.

The Phase1Client is a 3,218-line monolith that should be incrementally split into:
  - extraction_client.py:  run_step_build_*, bbox/action management, geography resolution
  - pipeline_client.py:    normalize, features, cluster, resolve, rank, promote wrappers
  - query_client.py:       All list_*/get_*/summary read methods
  - decision_client.py:    approve/reject, review requests, phase3 ambiguity resolution

For now, all methods remain in the parent client.py and this package
re-exports the unified class for forward-compatible imports.

Usage:
    from datamind_console.phases.phase1_nodes.clients import Phase1Client
"""
from datamind_console.phases.phase1_nodes.client import Phase1Client

__all__ = ["Phase1Client"]
