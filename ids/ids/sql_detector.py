import re

SQL_PATTERNS = [
    r"(\bOR\b|\bAND\b).*=.*",
    r"(\bUNION\b\s+\bSELECT\b)",
    r"(\bDROP\b\s+\bTABLE\b)",
    r"(\bINSERT\b\s+\bINTO\b)",
    r"(\bDELETE\b\s+\bFROM\b)",
    r"(\bUPDATE\b.+\bSET\b)",
    r"(--)",
]

def detect_sql_injection(text):
    for pattern in SQL_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False