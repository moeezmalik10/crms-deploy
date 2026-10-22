# HTTP DATASET CSIC 2010

Training data for R2 (explainable, request-level intrusion detection). `csic_database.csv`
itself is **not committed to this repo** (29.54MB, gitignored) — download it yourself, see below.

## Provenance

- **Original dataset**: "HTTP DATASET CSIC 2010", built by the Information Security Institute
  of CSIC (Spanish National Research Council) — Giménez, Villegas, Marañón et al. Generated
  traffic against a Spanish-language e-commerce test application ("tienda1"), labeled Normal or
  Anomalous (SQL injection, XSS, CRLF injection, buffer overflow, parameter tampering, static
  resource probing, and more).
- **Original hosting** (`tic.itefi.csic.es/dataset/`, formerly `isi.csic.es/dataset`) was
  unreachable both from this session's browser and confirmed down by the project owner directly
  as of 2026-10-21 - a known, long-standing reliability issue with this dataset's original host
  (also undiscoverable via the Wayback Machine: the HTML description page is archived, but the
  actual `.rar` data files were never crawled/archived).
- **Actual source used**: the Kaggle mirror
  [`ispangler/csic-2010-web-application-attacks`](https://www.kaggle.com/datasets/ispangler/csic-2010-web-application-attacks)
  (`csic_database.csv`, uploaded 2019, license "Data files © Original Authors" - the uploader
  converted the original `.txt` request dumps to CSV, keeping all fields, and says so directly
  on the dataset page rather than claiming original authorship).

## Why this mirror was trusted

Checked directly, not assumed, before use:
- Row count and class balance match the official description exactly: 61,065 rows, **36,000
  Normal / 25,065 Anomalous** (CSIC's own page: "36,000 normal requests and more than 25,000
  anomalous requests").
- Content matches the official description: Spanish-language e-commerce traffic
  (`tienda1`/`Vino Rioja` etc.), `localhost:8080` host, the exact attack categories CSIC
  describes.
- Parses cleanly end to end with `pandas.read_csv(..., encoding="latin-1")` (the dataset
  contains Latin-1 characters per CSIC's own description) - 61,065 rows, zero nulls in the
  `classification` column.
- The uploader's own description is explicit about being a derived, non-original conversion,
  not an attempt to misattribute authorship - the opposite of the licensing opacity this
  project's security notes elsewhere warn against (see `ids/ids/model_loader.py`'s own history).

## Columns

`Method, User-Agent, Pragma, Cache-Control, Accept, Accept-encoding, Accept-charset, language,
host, cookie, content-type, connection, lenght [sic, from the original], content,
classification, URL`. The first (unnamed) column duplicates `classification` as a human-readable
string (`"Normal"`/`"Anomalous"`) where `classification` itself is `0`/`1`.

## To re-obtain it

1. Log into Kaggle.
2. Download `csic_database.csv` from the dataset page linked above.
3. Place it at `ids/training_data/csic_2010/csic_database.csv` (this exact path - the training
   scripts expect it here).

## Citation (for the final report)

> C. Torrano-Giménez, A. Pérez-Villegas, G. Álvarez Marañón, "An Anomaly-based Approach for
> Intrusion Detection in Web Traffic," Journal of Information Assurance and Security, 2010.
> Dataset: HTTP DATASET CSIC 2010, Information Security Institute of CSIC.
> Accessed via Kaggle mirror (ispangler/csic-2010-web-application-attacks) on 2026-10-21, the
> original host (tic.itefi.csic.es/dataset) being unreachable at that time.
