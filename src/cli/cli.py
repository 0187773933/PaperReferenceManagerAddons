import argparse
from pathlib import Path
# import shutil
# import etms_downloader.tasks.tasks as tasks
import src.tasks.tasks as tasks
from pathlib import Path

def cli():
	parser = argparse.ArgumentParser()

	# Default
	parser.add_argument(
		"--output" ,
		type=Path ,
		default=Path.cwd().joinpath( "output" ) ,
		help="Output Location"
	)
	parser.add_argument(
		"--config" ,
		type=Path ,
		default=Path.cwd().joinpath( "config" ) ,
		help="Config Files Location"
	)
	parser.add_argument(
		"--searches" ,
		type=Path ,
		default=Path.cwd().joinpath( "searches" ) ,
		help="Searches Files Location"
	)
	parser.add_argument(
		"--mendeley-source" ,
		type=str ,
		default="api" ,
		help="Mendeley Source :: API OR Local SQLite ( encrypted , todo... )"
	)

	# Runtime
	parser.add_argument(
		"--manager" ,
		type=str ,
		default=None ,
		help="Mendeley/Zotero/EndNote/Paperpile/RefWorks/ReadCube"
	)
	parser.add_argument(
		"--mendeley" ,
		action="store_true",
		default=False ,
		help="Mendeley Reference Manage"
	)
	parser.add_argument(
		"--zotero" ,
		action="store_true",
		default=False ,
		help="Zotero Reference Manage"
	)

	# Optional
	parser.add_argument(
		"--zotero-sqlite" ,
		type=Path ,
		default=None ,
		help="Optionally Specify Direct Path to Zotero SQLite DB"
	)
	parser.add_argument(
		"--mendeley-sqlite" ,
		type=Path ,
		default=None ,
		help="Optionally Specify Direct Path to Mendeley SQLite DB"
	)
	parser.add_argument(
		"--top-author-count" ,
		type=int ,
		default=100 ,
		help="Optionally Specify Total Number of Most Common Authors"
	)

	# Other Tasks
	parser.add_argument(
		"--mendeley-download" ,
		action="store_true" ,
		default=False ,
		help="Download Mendeley PDFs"
	)

	args = parser.parse_args()
	if args.mendeley_download:
		tasks.mendeley_download( args )
	tasks.main( args )