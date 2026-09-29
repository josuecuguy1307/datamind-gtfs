"""datamind_console.persistence — canonical DB write wrappers.

The sole writer for route_prod.routes is `route_prod_writer.write_to_route_prod`.
Direct INSERT/UPDATE/DELETE on route_prod.* from application code is deprecated
and will be revoked at the DB level after Prompt 3.
"""

from datamind_console.persistence.route_prod_writer import (  # noqa: F401
    BulkWriteResult,
    WriteResult,
    delete_route_prod,
    delete_routes_prod_bulk,
    patch_route_prod_fields,
    prune_stop_node_id_from_routes,
    write_to_route_prod,
)
