"""Templates store — DB query helpers."""

from creditpaper.templates.store.template_store import (
    # papers
    add_paper,
    create,
    delete,
    delete_paper,
    get_by_id,
    get_default,
    list_for_tenant,
    # paper types
    list_paper_types,
    list_papers,
    set_active,
    update,
)

__all__ = [
    "add_paper",
    "create",
    "delete",
    "delete_paper",
    "get_by_id",
    "get_default",
    "list_for_tenant",
    "list_paper_types",
    "list_papers",
    "set_active",
    "update",
]
