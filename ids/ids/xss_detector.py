import re

XSS_PATTERNS = [
    r"<script.*?>.*?</script>",
    r"javascript:",
    r"onerror=",
    r"onload=",
    r"<iframe.*?>",
    r"<img.*?>",
    r"<svg.*?>"
]

def detect_xss(text):
    for pattern in XSS_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False