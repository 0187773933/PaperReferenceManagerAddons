"""
Opt-in live terminal UI for ` prma server --watch --tui ` .

Design constraints ( learned the hard way ) :

  * READ-ONLY OBSERVER. This runs in its own daemon thread and only *reads*
    thread-safe snapshots the worker already publishes ( ProcessWorker.snapshot_jobs
    / snapshot_batch ). It never calls into the processing path , so it cannot
    stall or break task processing.

  * NEVER TOUCHES sys.stdout / sys.stderr. rich's Live is created with
    redirect_stdout=False , redirect_stderr=False -- so the ML tasks / subprocesses
    keep their real file descriptors. ( Rich's default stdout redirect is the thing
    that broke processing before. )

  * FULL-SCREEN , htop-style. screen=True uses the alternate screen buffer : one
    fixed window redrawn in place , original terminal restored on exit.

If rich is missing or stdout isn't a TTY , server.run() never constructs this --
it stays on the classic line-by-line logs.
"""

import os
import sys
import time
import threading

from rich.live         import Live
from rich.layout       import Layout
from rich.console      import Console
from rich.panel        import Panel
from rich.table        import Table
from rich.text         import Text
from rich.progress_bar import ProgressBar


def _fmt_dur( sec ):
	sec = int( sec )
	if sec < 60:
		return f"{sec}s"
	m , s = divmod( sec , 60 )
	if m < 60:
		return f"{m}m{s:02d}s"
	h , m = divmod( m , 60 )
	return f"{h}h{m:02d}m"


def _pbar( completed , total , color="green" ):
	total = max( 1 , total )
	return ProgressBar( total=total , completed=completed ,
	                    complete_style=color , finished_style="green" ,
	                    style="grey30" , pulse=False )


class ServerTUI:

	def __init__( self , worker , header_lines , request_log=None ):
		self._worker  = worker
		self._header  = list( header_lines or [] )
		self._reqlog  = request_log
		self._console = Console( file=sys.__stdout__ , highlight=False , emoji=False )
		self._stop    = False
		self._thread  = None
		self._t_boot  = time.time()
		self._saved_out = None    # sys.stdout we swapped out ( restored on stop )
		self._saved_err = None
		self._devnull   = None

	# -- lifecycle -----------------------------------------------------------
	def start( self ):
		self._mute()
		self._thread = threading.Thread( target=self._run , daemon=True )
		self._thread.start()

	def stop( self ):
		self._stop = True
		t = self._thread
		if t:
			t.join( timeout=2.0 )
		self._unmute()

	# -- muting ( PYTHON level only -- never touches fd 1/2 ) -----------------
	def _mute( self ):
		"""Send main-process prints + tqdm bars ( e.g. the reindex's "References"
		bar ) to /dev/null so nothing paints under the dashboard. We reassign the
		sys.stdout / sys.stderr OBJECTS only ; the underlying file descriptors are
		untouched , so multiprocessing / subprocess OCR workers keep a real stderr
		and can't deadlock. The dashboard still renders because it draws via
		sys.__stdout__ , which Python never reassigns."""
		try:
			self._devnull   = open( os.devnull , "w" )
			self._saved_out = sys.stdout
			self._saved_err = sys.stderr
			sys.stdout = self._devnull
			sys.stderr = self._devnull
		except Exception:
			pass

	def _unmute( self ):
		if self._saved_out is not None:
			sys.stdout = self._saved_out
		if self._saved_err is not None:
			sys.stderr = self._saved_err
		if self._devnull is not None:
			try: self._devnull.close()
			except Exception: pass
		self._saved_out = self._saved_err = self._devnull = None

	def _run( self ):
		# The Live context owns the alternate screen ; exiting it ( when _stop is
		# set , or on any error ) restores the original terminal.
		try:
			with Live( self._render() , console=self._console , screen=True ,
			           auto_refresh=False , redirect_stdout=False ,
			           redirect_stderr=False ) as live:
				while not self._stop:
					try:
						live.update( self._render() , refresh=True )
					except Exception:
						pass                       # a render hiccup must never kill the UI
					time.sleep( 0.25 )
		except Exception:
			pass

	# -- state ( read-only snapshots the worker publishes ) ------------------
	def _snap( self ):
		w = self._worker
		try:
			jobs  = w.snapshot_jobs()  if w else {}
			batch = w.snapshot_batch() if w else None
		except Exception:
			jobs , batch = {} , None
		req = {}
		try:
			req = self._reqlog.snapshot() if self._reqlog else {}
		except Exception:
			req = {}
		return jobs or {} , batch , req or {}

	# -- rendering : one fixed full-screen layout ----------------------------
	def _render( self ):
		jobs , batch , req = self._snap()
		root = Layout()
		root.split_column(
			Layout( self._panel_header( batch ) , name="header" , size=len( self._header ) + 3 ) ,
			Layout( name="body" , ratio=1 ) ,
		)
		root[ "body" ].split_row(
			Layout( name="left" , ratio=1 ) ,
			Layout( name="right" , ratio=1 ) ,
		)
		root[ "left" ].split_column(
			Layout( self._panel_processing( jobs , batch ) , name="proc" , size=7 ) ,
			Layout( self._panel_queue( batch ) , name="queue" , ratio=1 ) ,
		)
		root[ "right" ].split_column(
			Layout( self._panel_requests( req ) , name="reqs"  , ratio=1 ) ,
			Layout( self._panel_recent( jobs )  , name="recent" , size=9 ) ,
		)
		return root

	def _panel_header( self , batch ):
		g = Table.grid( padding=( 0 , 1 ) , expand=True )
		g.add_column( ratio=1 )
		for i , ln in enumerate( self._header ):
			g.add_row( Text( ln , style=( "bold green" if i == 0 else "dim" ) , no_wrap=True ) )
		g.add_row( Text( f"up { _fmt_dur( time.time() - self._t_boot ) }" , style="cyan" ) )
		return Panel( g , title="PRMA server" , title_align="left" ,
		              border_style="green" , padding=( 0 , 1 ) )

	def _panel_processing( self , jobs , batch ):
		active = jobs.get( "active" )
		g = Table.grid( padding=( 0 , 1 ) , expand=True )
		g.add_column( justify="left" , no_wrap=True , width=6 , style="bold" )
		g.add_column( ratio=1 )                                         # the bar
		g.add_column( width=22 , no_wrap=True , overflow="ellipsis" )   # count / detail

		if not batch or not active:
			note = "scanning library…" if jobs.get( "scanning" ) else "waiting for new papers"
			g.add_row( "batch" , _pbar( 0 , 1 , "cyan" )  , Text( "idle" , style="dim" ) )
			g.add_row( "paper" , Text( note , style="dim" , no_wrap=True , overflow="ellipsis" ) , Text( "" ) )
			g.add_row( "stage" , _pbar( 0 , 1 , "green" ) , Text( "—" , style="dim" ) )
			return Panel( g , title="processing" , title_align="left" ,
			              border_style="grey37" , padding=( 0 , 1 ) )

		stages  = batch.get( "stages" ) or []
		nst     = max( 1 , len( stages ) )
		stage   = active.get( "stage" ) or ""
		stage_i = ( stages.index( stage ) + 1 ) if stage in stages else 0
		started = active.get( "started" ) or time.time()
		elapsed = _fmt_dur( time.time() - started )
		done , total = batch.get( "done" , 0 ) , batch.get( "total" , 1 )
		label = batch.get( "label" , "" )
		g.add_row( "batch" , _pbar( done , total , "cyan" ) ,
		           Text( f"{done}/{total} · {label}" , style="cyan" ) )
		g.add_row( "paper" ,
		           Text( active.get( "title" ) or "…" , overflow="ellipsis" , no_wrap=True ) ,
		           Text( "" ) )
		g.add_row( "stage" , _pbar( stage_i , nst , "green" ) ,
		           Text( f"{stage_i}/{nst} {stage or '…'} · {elapsed}" , style="green" ,
		                 overflow="ellipsis" , no_wrap=True ) )
		return Panel( g , title=f"processing · {label}" , title_align="left" ,
		              border_style="cyan" , padding=( 0 , 1 ) )

	def _panel_queue( self , batch ):
		t = Table.grid( padding=( 0 , 1 ) , expand=True )
		t.add_column( width=3 , justify="right" , style="dim" )
		t.add_column( ratio=1 )
		pending = []
		if batch:
			titles = batch.get( "titles" ) or []
			pending = titles[ batch.get( "i" , 0 ): ]     # not-yet-started papers
		for i , title in enumerate( pending[ :50 ] , 1 ):
			t.add_row( str( i ) , Text( title , overflow="ellipsis" , no_wrap=True ) )
		return Panel( t , title=f"queue · {len( pending )} pending" , title_align="left" ,
		              border_style="yellow" , padding=( 0 , 1 ) )

	def _panel_requests( self , req ):
		t = Table( expand=True , box=None , pad_edge=False , show_header=False )
		t.add_column( width=8 , style="dim" , no_wrap=True )            # time
		t.add_column( width=1 , no_wrap=True )                          # ✓ / ✗
		t.add_column( ratio=1 , no_wrap=True , overflow="ellipsis" )    # title / doi
		for ( ts , ex , title , doi ) in req.get( "rows" ) or []:
			mark  = Text( "✓" , style="bold green" ) if ex else Text( "✗" , style="bold red" )
			label = title or doi or "—"
			t.add_row( ts , mark ,
			           Text( label , style=( "green" if ex else "default" ) ,
			                 overflow="ellipsis" , no_wrap=True ) )
		head = ( f"incoming /exists · {req.get( 'served' , 0 )} served · "
		         f"{req.get( 'hits' , 0 )} hits" )
		return Panel( t , title=head , title_align="left" ,
		              border_style="grey37" , padding=( 0 , 1 ) )

	def _panel_recent( self , jobs ):
		t = Table( expand=True , box=None , pad_edge=False , show_header=False )
		t.add_column( width=1 , no_wrap=True )                          # ✓ / ✗
		t.add_column( ratio=1 , no_wrap=True , overflow="ellipsis" )    # title
		t.add_column( width=6 , justify="right" , style="dim" , no_wrap=True )   # secs
		for j in jobs.get( "recent" ) or []:
			ok    = j.get( "status" ) == "done"
			mark  = Text( "✓" , style="bold green" ) if ok else Text( "✗" , style="bold red" )
			dur   = ""
			if j.get( "finished" ) and j.get( "started" ):
				dur = _fmt_dur( j[ "finished" ] - j[ "started" ] )
			t.add_row( mark ,
			           Text( j.get( "title" ) or j.get( "key" ) or "—" ,
			                 style=( "default" if ok else "red" ) ,
			                 overflow="ellipsis" , no_wrap=True ) ,
			           dur )
		return Panel( t , title="recently processed" , title_align="left" ,
		              border_style="grey37" , padding=( 0 , 1 ) )
