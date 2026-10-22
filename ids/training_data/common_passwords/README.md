# Common password frequency corpus

Training data for R2's benign-password realism (see `docs/handoff/R2_explainable_ids_plan.md`
item 2c: the first synthetic password generator had its own implicit statistical "shape" that a
model could partly learn to recognize instead of generalizing to genuinely varied real
passwords - a 24.3% false-positive rate on independently-generated benign logins was the
measured result). `xato-net-10-million-passwords-100000.txt` itself is **not committed to this
repo** (764KB, gitignored) - download it yourself, see below.

## What this is, and how it's used

**Source**: [`danielmiessler/SecLists`](https://github.com/danielmiessler/SecLists), `Passwords/
Common-Credentials/xato-net-10-million-passwords-100000.txt` - the 100,000 most common passwords
from a well-known breach-derived corpus (the xato.net 10-million-password dataset), sorted most-
to-least frequent. SecLists is a standard, widely-used public security-research resource
(penetration testing, password-strength evaluation, and - as here - training data for defensive
ML security tools); this is the same category of legitimate defensive-security use as the CSIC
2010 dataset in `../csic_2010/`.

**Used only for its statistical shape** (length, character-class mix, common patterns like
word+digits) to generate realistic *synthetic* benign training examples - never to guess, test,
or attack any real account. `ids/training/simulate_attacks.py` samples from this list (weighted
toward the more common entries, matching the real-world Zipfian skew where a small number of
passwords account for a large share of real usage) to build benign login events, instead of the
original hand-rolled generator's narrow, easily-overfit small set of patterns.

## Why this mirror was trusted

- Downloaded directly from GitHub's raw content host (no login, no third-party mirror of unclear
  provenance - unlike CSIC 2010, this file needed no fallback source).
- 100,000 lines confirmed (`wc -l`), 781,879 bytes confirmed against the file's own
  `Content-Length` header before and after download - the file wasn't corrupted or truncated in
  transit.
- Spot-checked: top entries are well-known real-world most-common passwords ("123456",
  "password", "qwerty", "12345678"...), consistent with every other published analysis of this
  same well-known dataset.

## To re-obtain it

Download `xato-net-10-million-passwords-100000.txt` from the URL above and place it at
`ids/training_data/common_passwords/xato-net-10-million-passwords-100000.txt` (this exact path -
the training scripts expect it here).
