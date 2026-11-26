# Dataset Dictionary (`data/`)

This directory contains the open, sanitized snapshot exported from the **[Zotero Public Group (`6777746`)](https://www.zotero.org/groups/6777746/assassins_creed_research/library)** and permanently archived on Zenodo ([`10.5281/zenodo.24557853`](https://doi.org/10.5281/zenodo.24557853)).

## Files

| File | Format | Intended Use |
| :--- | :--- | :--- |
| `bibliography.csv` | UTF-8 CSV | Direct spreadsheet analysis, pandas/R scripts, and Hugging Face / Zenodo distribution |
| `bibliography.json` | Pretty JSON | Full structured catalog with `meta`, `taxonomy_tree`, `stats`, and `items` |

## Copyright & Privacy Safeguards

- **Bibliographic Metadata (CC0 1.0)**: Titles, authors, editors, publication years, venues, publishers, volume/issue/pages, and DOI/URL identifiers are factual metadata derived from OpenAlex and Crossref.
- **Curated Taxonomy & AI Triage Summaries (CC BY 4.0)**: Four-dimensional collection assignments (`Games`, `Subjects`, `Topics`, `Languages`), controlled tags, and original English `ai_summary` annotations.
- **Zero Publisher Abstracts or Full Texts**: Original `abstractNote` text and PDF attachments are strictly stripped from all downloadable files to respect publisher copyright. Always verify claims against the original full text before citing.
- **Zero Personal Credentials**: Internal Zotero user IDs and `createdByUser` fields are never exported.

## Field Schema (`bibliography.csv`)

| Column | Description |
| :--- | :--- |
| `zotero_key` | 8-character Zotero item identifier in the public group |
| `item_type` | Native Zotero type (`journalArticle`, `thesis`, `bookSection`, `conferencePaper`, `book`) |
| `title` | Normalized scholarly work title |
| `authors` / `editors` | Semicolon-separated normalized creator names |
| `year` | 4-digit publication year |
| `venue` | Sanitized journal title, book title, proceedings, university, or publisher |
| `publisher` / `volume` / `issue` / `pages` | Crossref-enriched citation details |
| `doi` / `url` | Normalized bare DOI and canonical publisher/source URL |
| `relevance` | Curated relevance level (`core` or `case_study`) |
| `doc_type` | Controlled document genre (`research-article`, `thesis`, `book-chapter`, etc.) |
| `citations` | OpenAlex citation count recorded at retrieval time |
| `games`, `subjects`, `topics`, `languages` | Semicolon-separated subcollections across the 4 taxonomy dimensions |
| `tags` | Semicolon-separated controlled tags (`rel:*`, `type:*`, `game:*`, `lang:*`, `#source/*`) |
| `ai_summary` | Original 1-sentence English triage summary written during LLM curation |

## Refreshing This Snapshot

The dataset is refreshed automatically via `.github/workflows/sync-catalog.yml` or manually with zero API keys:

```bash
python export_catalog.py --group-id 6777746 --skip-if-unchanged
```
