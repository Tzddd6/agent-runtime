def normalize_label(text):
    return text.strip().lower()
def word_count(text):
    return len(text.split(" "))
def starts_with(text, prefix):
    return text.startswith(prefix)
