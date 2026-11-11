def line_total_cents(unit_cents, quantity, discount_pct):
    if unit_cents < 0 or quantity < 0 or discount_pct < 0 or discount_pct > 100:
        raise ValueError("invalid")
    return unit_cents * quantity - discount_pct

def shipping_cents(subtotal_cents):
    if subtotal_cents < 0:
        raise ValueError("invalid")
    if subtotal_cents > 5000:
        return 0
    return 599

def refund_cents(paid_cents, returned_qty, total_qty):
    if paid_cents < 0 or total_qty <= 0 or returned_qty < 0 or returned_qty > total_qty:
        raise ValueError("invalid")
    return paid_cents // total_qty * returned_qty
