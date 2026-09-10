"""Read-only resolution of historical IDs, without changing historical prose."""

from .mirror_models import IdentityAlias


def resolve_ids(db, ids, kind="circular"):
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    aliases = dict(db.query(IdentityAlias.old_id, IdentityAlias.new_id).filter(
        IdentityAlias.kind == kind, IdentityAlias.old_id.in_(ids),
    ).all())
    return list(dict.fromkeys(aliases.get(item, item) for item in ids))


def resolve_id(db, identity, kind="circular"):
    return resolve_ids(db, [identity], kind)[0] if identity else identity
