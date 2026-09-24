/* ---------------------------------------------------------------------------
   common.js -- the helpers every prma page was writing out for itself.

   Served at GET /static/common.js and loaded with a plain <script src> BEFORE
   each page's own inline script , so these are ordinary globals the page code
   below it can just use. A page that loads this must NOT redeclare any of them
   ( a second ` const esc ` in a later script is a SyntaxError , not a shadow ).

   What belongs here : anything two pages would otherwise both spell out. What
   does not : a page's own rendering. dashboard.html and review.html each build
   their link row out of the primitives below rather than sharing one links()
   that has to know about both -- the primitives are the part that was actually
   the same.
--------------------------------------------------------------------------- */

/* The institution's ezproxy prefix. One string , five pages : this is the line
   to change if you fork prma for a different library. Server-side the same URL
   is built by _PROXY_URL_TEMPLATE in src/tasks/code.py. */
const PROXY = "https://doi-org.ezproxy.libraries.wright.edu/";

const $  = s => document.querySelector( s );
const $$ = s => Array.from( document.querySelectorAll( s ) );

/* HTML-escape anything bound for innerHTML. Every page builds its rows as
   template strings , so this is on the hot path of all of them. */
const esc = s => ( s == null ? "" : String( s ) ).replace( /[&<>"]/g, c =>
  ( { "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[ c ] ) );

const fmt = n => ( n || 0 ).toLocaleString();

/* created_at and friends are ISO-8601 stamps ( "2026-06-23T19:26:56+00:00" ).
   fmtDate keeps the date for a table cell ; when() keeps the whole thing but
   drops the T , for a tooltip or a status line. */
const fmtDate = s => s ? String( s ).slice( 0, 10 ) : "";
const when    = s => esc( String( s || "" ).replace( "T", " " ).replace( "+00:00", " UTC" ) );

/* The reader page for a paper. The key IS the DOI for anything with one ( see
   papers.record_key ) , so its slashes stay slashes in the path -- /md/10.1038/…
   -- the way the server and the figure reports both spell it. */
const mdHref = k => "/md/" + encodeURIComponent( k ).replace( /%2F/gi, "/" );

/* ---- link rows -----------------------------------------------------------
   The two anchors every surface offers for a DOI : the publisher's page , and
   the same page through the library proxy. Returned as an array so a caller can
   splice its own links in around them. */
function doiLinks( doi ){
  if( !doi ) return [];
  return [
    `<a href="https://doi.org/${esc( doi )}" target="_blank" rel="noopener" title="Publisher page">DOI</a>` ,
    `<a href="${PROXY}${esc( doi )}" target="_blank" rel="noopener" title="Through the library proxy">Proxy</a>` ,
  ];
}

/* Source-code / data links harvested by ` prma code ` from the paper's abstract
   + OCR full text ( src/tasks/code.py -> paper['code'].links , compacted for the
   browser by code.display_links ). Each renders as its host label -- GitHub /
   OSF / Dryad / … -- linking out to the repo.

   This is the ONE renderer for them : the dashboard's In-Library "Code" column
   and the /review link row both call it, off the same field, so a paper that
   ships code says so identically wherever you meet it.

   Empty -> a dim em-dash , so a column of them still reads as a column. A link
   ` prma code ` repaired from an OCR-mangled name carries `raw` : we mark those
   with a ˟ and spell out the original in the tooltip , so a wrong guess is easy
   to spot rather than silently followed. */
function codeLinks( list ){
  const ls = list || [];
  if( !ls.length ) return `<span class="muted">—</span>`;
  return ls.map( l => {
    const fixed = l.raw ? `${l.url}  ( OCR-repaired from ${l.raw} )` : l.url;
    const label = esc( l.source || "link" ) + ( l.raw ? `<sup class="muted">˟</sup>` : "" );
    return `<a href="${esc( l.url )}" target="_blank" rel="noopener" title="${esc( fixed )}">${label}</a>`;
  } ).join( " " );
}

/* ---- theme toggle --------------------------------------------------------
   The attribute is already on <html> before paint ( /static/boot.js ) ; this
   only labels the header button and persists a flip. The setting is shared by
   every prma page , so flipping it here flips the dashboard and both boards the
   next time you look at them. Call once, after the header exists. */
function initTheme(){
  const btn = $( "#toggleTheme" );
  if( !btn ) return;
  const cur   = () => document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  const label = () => { btn.textContent = cur() === "dark" ? "☀ Light" : "🌙 Dark"; };
  btn.addEventListener( "click", () => {
    const next = cur() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem( "prma-theme", next ); } catch( e ) {}
    label();
  } );
  label();
}

/* ---- the header's height, kept current -----------------------------------
   The two boards pin their column head straight under the page header
   ( top:var(--headh) ), so --headh has to be the header's height at EVERY
   moment -- not just the moments something remembered to measure it. A note
   line appearing, a row of buttons re-wrapping around a longer save stamp, the
   🔒 badge showing up : each one changes the height, and a stale value leaves
   the column head floating in a gap, or tucked half under the header. A
   ResizeObserver hears every one of them. Call once, after the header exists. */
function trackHeadHeight(){
  const el = $( "header" );
  if( !el ) return;
  const set = () => document.documentElement.style.setProperty( "--headh" , el.offsetHeight + "px" );
  set();
  if( window.ResizeObserver ) new ResizeObserver( set ).observe( el );
  else window.addEventListener( "resize" , set );
}

/* ===========================================================================
   LIST PAGES -- /code and /datasets
   ===========================================================================
   The same furniture with different columns : a filter chipbar , a sortable
   table of library papers , the four membership badges , a CSV / xlsx export
   and a toast. Everything below was spelled out twice ( and csvCell / stamp
   four and three times , counting the boards and /review ) before it moved
   here. The CSS half lives under ` :where(.list) ` in common.css ; a page opts
   into both with <body class="list">.

   NAMES ARE GLOBAL. Top-level const / let here share one lexical environment
   with every page's inline script , so a page that redeclares one of these with
   const is a SyntaxError , not a shadow. Function declarations are the
   exception -- they may be overridden , and toast() below IS : the two boards
   define their own undo-capable toast( msg , ms , label , onUndo ) , which
   legally wins for them because their script runs after this file.
--------------------------------------------------------------------------- */

/* ---- one paper's out-links ----------------------------------------------
   Every anchor a LIBRARY row offers , in the order the pages show them :
   publisher , proxy , the PDF the server streams , the rendered md , the figure
   montage ` prma images ` wrote. Returned as an array so a caller can splice
   its own in ; paperLinkRow wraps the common case.

   NOT what the boards' rowLinks() does -- theirs takes a board item and reads a
   META side-table for a paper that may not be in the library at all , and adds
   an OpenAlex and a Methods link. Different input , different answer , so they
   keep their own. */
function paperAnchors( r ){
  const out = doiLinks( r.doi );
  if( r.pdf )     out.push( `<a href="/pdf?key=${encodeURIComponent( r.key )}" target="_blank" rel="noopener">PDF</a>` );
  if( r.has_md )  out.push( `<a href="${mdHref( r.key )}" target="_blank" rel="noopener">MD</a>` );
  if( r.montage ) out.push( `<a href="${esc( r.montage )}" target="_blank" rel="noopener">Figures</a>` );
  return out;
}
function paperLinkRow( r ){
  const out = paperAnchors( r );
  return out.length ? `<div class="links rowlinks">${out.join( "" )}</div>` : "";
}

/* ---- the curated-surface badges -----------------------------------------
   Where a paper sits on /review , /sort , /tiers and the /images picks. One
   vocabulary , so a paper that is "included" reads identically on every page
   that joins against those surfaces. DASH is the dim em-dash a blank cell gets ,
   so a column of nothing still reads as a column. */
const REVIEW_BADGE = {
  included: `<span class="badge inrev">included</span>` ,
  excluded: `<span class="badge outrev">excluded</span>` ,
};
const SORT_BADGE = {
  list:   `<span class="badge onsort">on list</span>` ,
  staged: `<span class="badge">staged</span>` ,
};
const DASH = `<span class="dash">—</span>`;
const tierBadge = t => t ? `<span class="badge tier">${esc( t )}</span>` : DASH;
const figsBadge = n => n ? `<span class="badge figs">${n}</span>` : DASH;

/* ---- the sortable header ------------------------------------------------
   One <tr> of <th data-col>, with the arrow on the active column. `cols` is the
   page's own column list -- [ { id , label , num? } ] -- and the page binds the
   clicks , because what a second click on a column MEANS ( flip , or restart in
   that column's own interesting direction ) is the page's business. */
function sortHeader( cols , key , dir ){
  return `<tr>${ [ `<th class="num static" title="Position in the order below">#</th>` ].concat(
    cols.map( c => {
      const ar = c.id === key ? `<span class="arrow">${dir === "asc" ? "▲" : "▼"}</span>` : "";
      return `<th class="${c.num ? "num" : ""}" data-col="${c.id}">${esc( c.label )}${ar}</th>`;
    } ) ).join( "" )}</tr>`;
}

/* ---- the filter chipbar -------------------------------------------------
   Draws one chip per item into `el` and wires the clicks. `items` is
   [ { id , label , n? , title? } ] , `on` says which are lit , and `toggle` is
   called with the id that was clicked -- the page owns the state , this owns
   the markup. Empty items clears the bar , which is how a host row with nothing
   to show disappears instead of leaving a stray label. */
function renderChipbar( el , label , items , on , toggle ){
  if( !el ) return;
  el.innerHTML = items.length
    ? ( label ? `<span class="lbl">${esc( label )}</span>` : "" ) + items.map( it =>
        `<span class="chip ${on( it.id ) ? "on" : ""}" data-chip="${esc( it.id )}"` +
        ( it.title ? ` title="${esc( it.title )}"` : "" ) + `>${esc( it.label )}` +
        ( it.n == null ? "" : `<span class="n">${fmt( it.n )}</span>` ) + `</span>` ).join( "" )
    : "";
  Array.from( el.querySelectorAll( ".chip" ) ).forEach( c =>
    c.onclick = () => toggle( c.dataset.chip ) );
}

/* ---- a forgiving substring test ------------------------------------------
   A title pasted out of a PDF rarely matches the stored one byte for byte : a
   curly ’ where the record has ' ( or none ) , a line break mid-title , an
   accent. foldText is utils.normalize_title with the spaces dropped as well , so
   only the letters and digits get compared. textHas keeps the plain substring
   test too , so a query that is mostly punctuation ( "c++" ) still means what
   it says. */
const foldText = s => String( s || "" ).normalize( "NFKD" ).toLowerCase().replace( /[^a-z0-9]+/g , "" );
function textHas( hay , q ){
  hay = String( hay || "" ).toLowerCase();
  q   = String( q || "" ).trim().toLowerCase();
  if( !q || hay.includes( q ) ) return true;
  const fq = foldText( q );
  return !!fq && foldText( hay ).includes( fq );
}

/* ---- the search box -----------------------------------------------------
   The debounce plus the two keys every list page binds : ` / ` to focus from
   anywhere , Escape to clear and blur. Opt-in ( a page CALLS this ) rather than
   bound on load , so the pages that already own those keys are untouched.
   `onChange` gets the trimmed , lowercased query. */
function bindSearch( sel , onChange ){
  const box = $( sel );
  if( !box ) return;
  let t = null;
  box.addEventListener( "input" , e => {
    clearTimeout( t );
    t = setTimeout( () => onChange( e.target.value.trim().toLowerCase() ) , 150 );
  } );
  document.addEventListener( "keydown" , e => {
    if( e.key === "/" && document.activeElement !== box ){ e.preventDefault(); box.focus(); }
    if( e.key === "Escape" && document.activeElement === box ){
      box.value = ""; box.blur(); onChange( "" );
    }
  } );
}

/* ---- the review's own state ---------------------------------------------
   Both list pages carry Review columns read off the last BUILD , so both have
   to say when there isn't one yet or the boards have moved since. One sentence ,
   said the same way. */
function reviewNote( rv ){
  rv = rv || {};
  return ( !rv.available
      ? `<span>the review columns are empty because no review has been built yet — ` +
        `<a href="/review">build one</a></span>`
      : rv.stale
        ? `<span>⚠ the boards have moved since the review was built — ` +
          `<a href="/review">rebuild it</a> to re-check them</span>` : "" ) +
    ( rv.generated ? `<span class="muted">review built ${esc( rv.generated.replace( "T" , " " ) )}</span>` : "" );
}

/* ---- transient message --------------------------------------------------
   Creates its own element and removes it ; the page needs no markup for it ,
   only the .toast rule in common.css. Overridden on /sort and /tiers , which
   want an Undo button on theirs. */
let _toastTimer = null;
function toast( msg ){
  let el = $( ".toast" );
  if( !el ){ el = document.createElement( "div" ); el.className = "toast"; document.body.appendChild( el ); }
  el.textContent = msg;
  clearTimeout( _toastTimer );
  _toastTimer = setTimeout( () => el.remove() , 2600 );
}

/* ---- handing the browser a file ------------------------------------------
   NOT called save() : both boards already have a save() that means "POST this
   document to the server" , and that is the more important verb to leave alone. */
function saveBlob( blob , name ){
  const a = document.createElement( "a" );
  a.href = URL.createObjectURL( blob );
  a.download = name;
  document.body.appendChild( a ); a.click();
  setTimeout( () => { URL.revokeObjectURL( a.href ); a.remove(); } , 1000 );
}
const stamp = () => new Date().toISOString().slice( 0 , 10 );

/* ---- CSV ----------------------------------------------------------------
   csvCell was written out on four pages ( both boards , /code , /datasets ) --
   quote anything containing a comma , a quote or a newline , and double the
   quotes inside. csvText assembles a whole file from a header and rows ; the
   boards build their lines themselves and just use the cell. */
const csvCell = v => {
  // ONE ROW PER RECORD. A Code or Datasets cell holds its links one per line ,
  // which is right on the board and wrong in a spreadsheet : quoted line breaks
  // are honoured there , so a single paper listing 496 archives drew a row 496
  // lines tall and the sheet came out ragged. They flatten to "; " -- the
  // separator the Tags column in the same file already uses -- so every row is
  // one uniform line. The BOARD keeps its line breaks ; this is the export
  // talking. ( Re-importing such a file brings those cells back joined with
  // "; " rather than one per line -- and only where you ask the import to
  // overwrite , since filling empty cells leaves a written one alone. )
  const s = ( Array.isArray( v ) ? v.join( "; " ) : v == null ? "" : String( v ) )
    .replace( /\r\n?/g , "\n" ).split( "\n" ).map( t => t.trim() ).filter( Boolean ).join( "; " );
  return /[",]/.test( s ) ? '"' + s.replace( /"/g , '""' ) + '"' : s;
};
const csvText = ( head , rows ) =>
  [ head.map( csvCell ).join( "," ) ].concat( rows.map( r => r.map( csvCell ).join( "," ) ) ).join( "\n" );

/* ---- handing over a CSV -------------------------------------------------
   The BOM is the whole point of this being a function. Our text is UTF-8 and
   always was , but a .csv carries no declaration of that : Excel opens one in
   the machine's legacy encoding unless the first bytes say otherwise , so on a
   Mac every em dash we write came back as ` ‚Äî ` ( EF BB BF absent , E2 80 94
   read as Mac Roman ) and every accented author name the same way. U+FEFF in
   front is what tells it -- and any reader that doesn't want one strips it ,
   which is what our own parseCSV does on the way back in.

   Every CSV the dashboard hands over goes through here , so none of them can
   drift back to raw UTF-8. NOT for the .json exports : a BOM in front of JSON
   is a parse error in anything strict. */
function saveCSV( text , name ){
  saveBlob( new Blob( [ "\uFEFF" + text ] , { type: "text/csv;charset=utf-8" } ) , name );
}

/* ---- the server-built workbook ------------------------------------------
   Both list pages export the CURRENT VIEW as .xlsx the same way : POST the keys
   the page is showing , in the order it is showing them , and hand back the
   blob. Server-side so the sheet carries real hyperlinks and no page has to
   carry a spreadsheet library. `keys` is a function , called at click time , so
   it always reads whatever is on screen NOW. */
function bindXlsxExport( btnSel , url , nameFn , keys ){
  const btn = $( btnSel );
  if( !btn ) return;
  btn.addEventListener( "click" , async () => {
    const ks = keys();
    if( !ks.length ){ toast( "Nothing to export" ); return; }
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = "⇧ …";
    try {
      const res = await fetch( url , {
        method: "POST" , headers: { "Content-Type": "application/json" } ,
        body: JSON.stringify( { keys: ks } ) ,
      } );
      if( !res.ok ) throw new Error( ( await res.json() ).error || res.statusText );
      saveBlob( await res.blob() , nameFn() );
      toast( `${ks.length} papers exported` );
    } catch( e ){
      toast( `Export failed: ${e.message || e}` );
    } finally {
      btn.disabled = false; btn.textContent = label;
    }
  } );
}

/* The empty state while the dashboard index is still building. Both list pages
   are views OVER that index , so both wait on it and both should say so the
   same way. */
const indexBuildingHtml = DATA =>
  `<div class="empty">Building the library index… ${esc( ( DATA || {} ).message || "" )}<br>` +
  `<span class="muted">Every column here is read off that index ; this page picks up when it lands.</span></div>`;
