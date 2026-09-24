/* ---------------------------------------------------------------------------
   boot.js -- restore the saved theme BEFORE first paint.

   Loaded synchronously from <head>, above the stylesheet, on every prma page :

     <script src="/static/boot.js"></script>

   A theme applied in DOMContentLoaded is a theme you WATCH being applied -- the
   page paints light and then goes dark. A blocking script in the head sets the
   attributes the palette keys off ( see /static/common.css ) before anything is
   drawn , so there is no flash.

   Both settings are shared by every page through localStorage , which is why
   flipping the theme on one tab and opening another gets you the same theme :
     prma-theme  "light" | "dark"    the header toggle
     prma-skin   "pastel"            the alternate palette
   localStorage throws in a few browser configurations , so the whole thing sits
   in a try/catch that falls back to light rather than leaving an unstyled page.
--------------------------------------------------------------------------- */
try {
  document.documentElement.dataset.theme = localStorage.getItem( "prma-theme" ) || "light";
  if( localStorage.getItem( "prma-skin" ) === "pastel" )
    document.documentElement.dataset.skin = "pastel";
} catch( e ) {
  document.documentElement.dataset.theme = "light";
}
