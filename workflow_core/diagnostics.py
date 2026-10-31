"""Bounded verification failure evidence, with common credential forms removed."""

import re


def redact(text):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
                  "[REDACTED PRIVATE KEY]", text, flags=re.S)
    text = re.sub(r"(?i)(\b(?:authorization\s*[:=]\s*(?:basic\s+|bearer\s+)?|bearer\s+))[^\s,;]+",
                  r"\1[REDACTED]", text)
    text = re.sub(r"(?i)([\"']?\b(?:[\w-]*(?:token|password|passwd|secret|api[_-]?key|access[_-]?key)[\w-]*)[\"']?\s*[:=]\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;&]+)",
                  r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|AKIA[A-Z0-9]{16})\b",
                  "[REDACTED]", text)
    text = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32)


def failure_evidence(stdout, stderr, reason=None):
    parts = []
    # Sanitize before limiting so truncation does not cut off a secret delimiter.
    for name, value in (("stderr", stderr), ("stdout", stdout)):
        if value and value.strip():
            parts.append((name, redact(value)))
    text = "\n".join("[" + name + "]\n" + value for name, value in parts)
    lines = text.splitlines()
    important = [line.strip() for line in lines
                 if re.search(r"(?:\b(?:\w*Error|\w*Exception)\b\s*:|^(?:ERROR|FAIL|FAILED|error):?)", line)]
    headline = important[-1] if important else reason or "Verification command returned a non-zero exit code"
    # Prefer a concrete exception over unittest's final failure count.
    exceptions = [line for line in important if re.search(r"\b\w*(?:Error|Exception)\s*:", line)]
    if exceptions:
        headline = exceptions[-1]
    excerpt = text[-3000:]
    return {"summary": headline[:300], "excerpt": excerpt, "truncated": len(text) > 3000,
            "redacted": any(original and redact(original) != original for original in (stdout, stderr)),
            "streams": [name for name, _ in parts]}
