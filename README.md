# Paper Reference Manager Addons

## Addons

1. `prma` : updates local snapshot
1. `prma missing` : generates missing.xlsx
2. `prma server` : runs the local HTTP server — the 'exists' endpoint for browser userscripts (`POST /exists`) **and** the full-text-search dashboard (`GET /`) for the **external papers you don't have**: works your library cites but you're missing, and works that cite your library. (Your own papers live in Zotero — they're indexed only for identity, to know what to exclude.) The dashboard serves the index that `prma reindex` persists to disk: it loads instantly and auto-reloads when you re-run reindex. If no index exists yet, it builds one lazily on first open.
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