def overlap_seconds(start_a, end_a, start_b, end_b):
    if end_a < start_a or end_b < start_b:
        raise ValueError("invalid")
    return min(end_a, end_b) - max(start_a, start_b)
def bucket_index(timestamp, origin, width):
    if width <= 0:
        raise ValueError("invalid")
    return timestamp // width - origin // width
def remaining_ttl(now, created, ttl):
    if ttl < 0:
        raise ValueError("invalid")
    return max(0, ttl - (now - created))
