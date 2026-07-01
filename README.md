# Paper Reference Manager Addons

## Addons

1. `prma` : alias for `prma server --watch` — launches the live server (exists endpoint + dashboard) with the background auto-processing worker on. (Run `prma server` without bare-command for the server with `--watch` off.)
1. `prma main` : the base pipeline — refresh the snapshot, then update the OpenAlex cache (meta + cited-by + references) for **every** library paper. This was the old no-subcommand default.
1. `prma missing` : generates missing.xlsx
2. `prma server` : runs the local HTTP server — the 'exists' endpoint for browser userscripts (`POST /exists`) **and** the full-text-search dashboard (`GET /`) for the **external papers you don't have**: works your library cites but you're missing, and works that cite your library. (Your own papers live in Zotero — they're indexed only for identity, to know what to exclude.) The dashboard serves the index that `prma reindex` persists to disk: it loads instantly and auto-reloads when you re-run reindex. If no index exists yet, it builds one lazily on first open. Pass `--watch` for **live processing**: a background worker detects papers you add to Zotero/Mendeley, runs the full per-paper suite on each (openalex → yolo → ocr → images → methods → code → md), and rebuilds the index so new papers appear automatically — the dashboard shows a live progress toast (polled from `GET /api/jobs`). Add `--watch-summarize` to also run the LLM summary on each.
3. `prma reindex` : refreshes the OpenAlex cache (snapshot + fetch new papers, download only), then (re)builds the dashboard's **own** full-text search index by streaming the OpenAlex cache off disk, and persists it. Independent of `missing.xlsx`. Incremental — adding papers only re-reads the new papers + their new references, not everything (`--full` to rebuild from scratch, `--skip-snapshot` to index the existing cache without re-fetching).
4. `prma snapshot` : creates local archive file
5. `prma yolo` : run doclayout yolo model on each pdf
6. `prma images` : generate individual images and a montage from figures and tables in each pdf
7. `prma ocr` : run ocr task on yolo blocks
8. `prma md` : generate markdown versions of each pdf
9. `prma text` : generate text versions of each pdf
10. `prma methods` : isolates methods sections of each pdf
11. `prma summarize` : runs summarization task on each pdf or section
12. `prma rollup` : generates a report from summarization tasks
13. `prma crawl` : attempts to crawl the OpenAlex graph
14. `prma code` : scans each paper's OpenAlex abstract + OCR text for **source-code / data links** (GitHub, GitLab, Bitbucket, OSF, Dryad, Zenodo, Figshare, Code Ocean, Hugging Face, …), reconstructing URLs the OCR split across lines and dropping dead/duplicate fragments. Pins the unique links on each record so the dashboard shows a sortable **Code** column (after Links, In-Library tab) and `prma md` can render a `## Source Code` section, and rolls everything up into `output/code/code.xlsx` (three sheets: *Links by Paper*, *By Paper*, and *Unique Links* — the last aggregating each link to the DOIs that cite it + a paper count; every per-paper sheet carries DOI / proxy / pdf links plus date-added and publication date). **GitHub enrichment:** if `config.yaml` has a `github.api_key`, it also fetches every unique GitHub repo's metadata + README (cached under `output/cache/github/`) and tags each by neuroimaging method (**fMRI / EEG / ECoG / fNIRS / MEG / PET / EMG**) — the *Unique Links* sheet gets a column per method (plus a combined *Methods* column on every sheet) so you can sort/filter to the fMRI repos first. Runs OCR inline (idempotent) so it's one-stop; `--force` re-scans every paper and re-fetches GitHub details. Also runs automatically as the `code` stage of the per-paper suite (see below), so papers added under `prma server --watch` are scanned on the fly.
15. `prma process <doi-or-title>` : find ONE paper in your manager (by exact DOI or fuzzy title) and run the full per-paper suite on just it — snapshot → openalex → yolo → ocr → images → methods → code → md — then reindex so the dashboard refreshes. The library tasks are idempotent and scoped to the single paper, so this is fast. `--summarize` also runs the LLM summary. Same code path the server's `--watch` worker uses per newly-added paper.

## Todo

- [ ] support non-doi items
- [ ] add .bib / .ris options
	- https://github.com/sciunto-org/python-bibtexparser
	- https://github.com/MrTango/rispy
- [ ] Web of Science Integration
	- https://developer.clarivate.com/applications
	- https://developer.clarivate.com/apis/wos

## Misc

- /Users/morpheous/.paddlex/official_models/PP-OCRv5_server_det
- /Users/morpheous/.paddlex/official_models/en_PP-OCRv5_mobile_rec
- https://help.oclc.org/Library_Management/EZproxy/EZproxy_configuration/Starting_point_URLs_and_config_txt
- https://libraries.wright.edu/sites/libraries.wright.edu/files/2022-07/pilot-linking-instructions_1.pdf
- https://help.oclc.org/Library_Management/EZproxy/EZproxy_database_stanzas/Database_stanzas
- https://help.oclc.org/Library_Management/EZproxy/EZproxy_database_stanzas/Database_stanzas/EZproxy_database_stanzas_-_All