from app.errors import AppError


def redistribute(offers):
    """Целые проценты: остаток получают офферы с меньшим ID, сумма ровно 100."""
    active = sorted((o for o in offers if not o.removed and o.remote_state == "active"), key=lambda o: o.offer_id)
    for o in offers:
        if o.removed:
            o.share, o.pinned = 0, False
    if not active:
        return
    fixed = sum(o.share for o in active if o.pinned)
    if fixed > 100:
        raise AppError("Сумма закреплённых долей превышает 100%.")
    free = [o for o in active if not o.pinned]
    if not free:
        if fixed != 100:
            raise AppError("Если закреплены все офферы, их доли должны давать 100%.")
        return
    share, remainder = divmod(100 - fixed, len(free))
    for index, offer in enumerate(free):
        offer.share = share + (index < remainder)
