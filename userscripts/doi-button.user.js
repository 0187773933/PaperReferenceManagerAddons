// ==UserScript==
// @name         DOI Button
// @namespace    asdf
// @version      0.18
// @description  Adds DOI Buttons
// @author       asdf
// @updateURL    https://github.com/0187773933/ZoteroExistsServer/raw/refs/heads/master/doi-button.user.js
// @downloadURL  https://github.com/0187773933/ZoteroExistsServer/raw/refs/heads/master/doi-button.user.js
// @grant        GM.xmlHttpRequest
// @connect      127.0.0.1

// @match        *://*/*

// ==/UserScript==

(function () {
    const DOMAIN_BLACKLIST = [ "docs.google.com" , "pilot.wright.edu" , "auth.wright.edu" , "google.com/maps" ];
	// ---------------------------------------------------------------------------
	// Site allow-list. Header uses a single  @match *://*/*  and we gate at runtime
	// instead of maintaining hundreds of @match lines. Edit DOI_ALLOWED_DOMAINS to
	// add/remove sites. Matches the domain itself and any subdomain.
	// ---------------------------------------------------------------------------
	const DOI_ALLOWED_DOMAINS = [
		"80.82.77.83","aaas.org","aacrmeetingabstracts.org","aaiddjournals.org",
		"aanda.org","aapgbulletin.datapages.com","aas.aanda.org","aasv.org",
		"academic.mintel.com","accessible.com","aclanthology.org","acm.org",
		"acs.org","adisonline.com","adsabs.harvard.edu","adswww.harvard.edu",
		"advan.physiology.org","aeaweb.org","agronomy-journal.org","agu.org",
		"ahiv.alexanderstreet.com","aiaa.org","aimsciences.org","aip.org",
		"aip.scitation.org","ajcn.org","ajcp.ascpjournals.org","ajevonline.org",
		"ajh.sagepub.com","ajhpcontents.com","ajp.psychiatryonline.org","ajpcell.physiology.org",
		"ajpendo.physiology.org","ajpgi.physiology.org","ajph.aphapublications.org","ajpheart.physiology.org",
		"ajplegacy.physiology.org","ajplung.physiology.org","ajpregu.physiology.org","ajprenal.physiology.org",
		"ajrccm.atsjournals.org","ajrcmb.org","ajslp.asha.org","ajsonline.org",
		"ajtmh.org","ala.org","als.dukejournals.org","americana.ncsu.edu",
		"americanliterature.dukejournals.org","americanspeech.dukejournals.org","amjbot.org","ams.org",
		"amsciepub.com","analusis.edpsciences.org","anb.org","andrologyjournal.org",
		"animres.edpsciences.org","annals.org","annee-philologique.com","annphys.org",
		"annualreviews.org","anthrosource.net","apex.ipap.jp","apidologie.org",
		"app.harpweek.com","appliedradiology.com","apps.isiknowledge.com","aps.org",
		"apsjournals.apsnet.org","arch-anim-breed.net","archderm.ama-assn.org","archinte.ama-assn.org",
		"archive.pepublishing.com","archives.chadwyck.com","archneur.ama-assn.org","archopht.ama-assn.org",
		"archpsyc.ama-assn.org","archsurg.ama-assn.org","arjournals.annualreviews.org","artstor.org",
		"arxiv.org","asadl.org","asae.frymulti.com","ascelibrary.org",
		"asianannals.ctsnetjournals.org","aslo.org","asm.org","aspet.org",
		"aspresolver.com","atypon-link.com","aviationweek.com","avmajournals.avma.org",
		"baidu.com","bas.umdl.umich.edu","bbmt.org","bepress.com",
		"berrymaninstitute.org","bing.com","biochemj.org","biochemsoctrans.org",
		"biol.uni.wroc.pl","biolbull.org","biolreprod.org","biomedcentral.com",
		"bioon.com","bioon.com.cn","bioone.org","bioscirep.org",
		"biotechniques.com","blackwell-synergy.com","bloodjournal.hematologylibrary.org","bmj.com",
		"bna.birds.cornell.edu","bos.frb.org","botany.org","boundary2.dukejournals.org",
		"britannica.com","bsas.org.uk","bssaonline.org","bul.sagepub.com",
		"businessweek.com","cabdirect.org","cabi.org","cambridge.org",
		"cameraobscura.dukejournals.org","cancerres.aacrjournals.org","canreviews.aacrjournals.org","carbo.chemnetbase.com",
		"cawq.ca","ccd.chemnetbase.com","ccl.sagepub.com","cell.com",
		"cgd.aacrjournals.org","checkpoint.riag.com","chelonianjournals.org","chemnetbase.com",
		"chestjournal.chestpubs.org","chicagomanualofstyle.org","china.eastview.com","choicesmagazine.org",
		"chronicle.com","ci.nii.ac.jp","ciaonet.org","cindasdata.com",
		"ciw.edu","cjc-online.ca","cl.uwpress.org","clevelandfed.org",
		"clincancerres.aacrjournals.org","clinchem.org","clinmed.netprints.org","clinsci.org",
		"cms.math.ca","cn-ki.net","cnki.com.cn","cnki.en.eastview.com",
		"cnki.net","collections.chadwyck.com","communicationencyclopedia.com","complit.dukejournals.org",
		"computerworld.com","consumerinterests.org","contemporaryobgyn.net","content.karger.com",
		"corporateaffiliations.com","cqvip.com","crcnetbase.com","crl.acrl.org",
		"crln.acrl.org","cro2.org","cshmonographs.org","cshprotocols.cshlp.org",
		"csi.sagepub.com","csiro.au","csis.cn","dandini.emeraldinsight.com",
		"darwin.edu.ar","db.chemsources.com","dccc.chemnetbase.com","dev.biologists.org",
		"dfc.chemnetbase.com","dichtung-digital.de","digital.library.mcgill.ca","digitalmicrofilm.proquest.com",
		"dioc.chemnetbase.com","direct.mit.edu","discovermagazine.com","dl.acm.org",
		"dl.begellhouse.com","dlib.eastview.com","dmd.aspetjournals.org","dmnp.chemnetbase.com",
		"doi.org","dx.doi.org","ebm.rsmjournals.com","ebook.rsc.org",
		"economist.com","edgj.org","edm.sagepub.com","edpsciences.org",
		"edrv.endojournals.org","edu","edu.cn","educationbook.aacrjournals.org",
		"eebo.chadwyck.com","eenews.net","ehq.sagepub.com","ejorel.com",
		"electrochem.org","elementsmagazine.org","elifesciences.org","els.net",
		"ema.sagepub.com","emeraldinsight.com","ems-ph.org","en.cnki.com.cn",
		"en.wikipedia.org","endo.endojournals.org","engineeringvillage2.com","enterprise.astm.org",
		"epirev.oupjournals.org","epjap.org","epubs.ans.org","er.uwpress.org",
		"erc.endocrinology-journals.org","erg.sagepub.com","esa.publisher.ingentaconnect.com","esajournals.org",
		"escholarship.org","etde.org","ethnohistory.dukejournals.org","europepmc.org",
		"europhysicsnews.org","evolutionary-ecology.com","exacteditions.com","extensionreport.osu.edu",
		"facs.org","familiesinsociety.org","fao.org","fasebj.org",
		"fhs.dukejournals.org","fiaf.chadwyck.com","find.acacamps.org","find.galegroup.com",
		"firstsearch.oclc.org","frontiersin.org","fundingopps2.cos.com","futuremedicine.com",
		"fyesit.metapress.com","gateway.proquest.com","genesdev.cshlp.org","genetics.org",
		"genome.cshlp.org","genomebiology.com","geology.gsapubs.org","giorgio.ingentaselect.com",
		"global-sci.com","glq.dukejournals.org","gmr.minsocam.org","google.com",
		"google.com.co.uk","google.com.hk","google.nl","gpoaccess.gov",
		"groveart.com","grovemusic.com","gsabulletin.gsapubs.org","gse-journal.org",
		"gut.bmj.com","hahr.dukejournals.org","hapi.gseis.ucla.edu","heart.bmj.com",
		"heinonline.org","hh.um.es","hope.dukejournals.org","hortsci.ashspublications.org",
		"horttech.ashspublications.org","hsus.cambridge.org","hti.umich.edu","hull.ac.uk",
		"ias.ac.in","ibe.sagepub.com","ibisworld.com","ibra.org.uk",
		"icevirtuallibrary.com","icf.uab.es","ici.org","ida.liu.se",
		"ieee.org","ieeexplore.ieee.org","ihserc.com","iimp.chadwyck.com",
		"ijc.org","ijdb.ehu.es","ijee.ie","ijs.sgmjournals.org",
		"impublications.com","inda.org","informahealthcare.com","informaworld.com",
		"infotrac.galegroup.com","infoweb.newsbank.com","ingentaconnect.com","ingentaselect.com",
		"inpractice.bmj.com","inscribe.iupress.org","inspirehep.net","int-res.com",
		"interfaces.journal.informs.org","interscience.wiley.com","iop.org","iopscience.iop.org",
		"iovs.org","ipap.jp","ir.uiowa.edu","isca-archive.org",
		"isiknowledge.com","isr.journal.informs.org","itergateway.org","itn.is",
		"iucn.org","iupac.org","iwaponline.com","j-csam.org",
		"jaaha.org","jama.ama-assn.org","jap.physiology.org","japr.fass.org",
		"jas.fass.org","jbc.org","jbjs.org","jbmronline.org",
		"jcb.rupress.org","jcem.endojournals.org","jco.ascopubs.org","jcp.bmj.com",
		"jcs.biologists.org","jeb.biologists.org","jem.rupress.org","jgp.rupress.org",
		"jgs.lyellcollection.org","jgslegacy.lyellcollection.org","jhortscib.org","jhr.uwpress.org",
		"jhse.org","jimmunol.org","jleukbio.org","jlr.org",
		"jme.endocrinology-journals.org","jmems.dukejournals.org","jmir.org","jmm.sgmjournals.org",
		"jn.nutrition.org","jn.physiology.org","jneurosci.org","jnm.snmjournals.org",
		"jnnp.bmj.com","jnrlse.org","joa.isa-arbor.com","joc.journal.informs.org",
		"joe.endocrinology-journals.org","john-libbey-eurotext.fr","journal.ashspublications.org","journal.telospress.com",
		"journalofinfection.com","journals.ametsoc.org","journals.aps.org","journals.cambridge.org",
		"journals.hil.unb.ca","journals.humankinetics.com","journals.iucr.org","journals.lww.com",
		"journals.naspa.org","journals.sagamorepub.com","journals.tdl.org","journalstp.gracescientific.com",
		"jove.com","jp.physoc.org","jpet.aspetjournals.org","jpsj.ipap.jp",
		"jsad.com","jsedres.sepmonline.org","jslhr.asha.org","jstage.jst.go.jp",
		"jstor.org","jswconline.org","jwildlifedis.org","jyi.org",
		"karger.com","kluwerlawonline.com","kluweronline.com","knovel.com",
		"la.rsmjournals.com","labanimal.com","labor.dukejournals.org","landesbioscience.com",
		"le.uwpress.org","lexis-nexis.com","lexisnexis.com","library.cqpress.com",
		"library.pressdisplay.com","library.seg.org","libraryissues.com","liebertonline.com",
		"link.springer-ny.com","link.springer.de","links.jstor.org","livestockscience.com",
		"livingbird.org","lj.uwpress.org","mansci.journal.informs.org","mapress.com",
		"math.ualberta.ca","mcponline.org","mcr.aacrjournals.org","mcr.sagepub.com",
		"medrxiv.org","medscimonit.com","mend.endojournals.org","metapress.com",
		"metla.fi","mic.sgmjournals.org","millerpublishing.com","minsocam.org",
		"mitpressjournals.org","mktsci.journal.informs.org","mlajournals.org","mluri.sari.ac.uk",
		"mmm.edpsciences.org","molbiolcell.org","molpharm.aspetjournals.org","mor.journal.informs.org",
		"mp.bmj.com","mp.weixin.qq.com","mq.dukejournals.org","mr-gut.cn",
		"msp.berkeley.edu","msucares.com","muse.jhu.edu","museumoftheearth.org",
		"mycologia.org","myinsight.ihsglobalinsight.com","nactateachers.org","nationaljournal.com",
		"nature.com","nber.org","nc-apa.org","ncbi.nlm.nih.gov",
		"ncbiotech.org","nccsdataweb.urban.org","ncdjjdp.org","ncjrs.org",
		"nclive.org","ncph.org","ncpublicschools.org","ncsu.naxosmusiclibrary.com",
		"ncte.org","nejm.org","netadvantage.standardandpoors.com","netlibrary.com",
		"neurology.org","new.sourceoecd.org","news.reseau-concept.net","ngc.dukejournals.org",
		"nho.sagepub.com","nonlin-processes-geophys.net","novel.dukejournals.org","npprj.spci.se",
		"nrcresearchpress.com","nsarchive.chadwyck.com","nsrl.ttu.edu","nucl.annualreviews.org",
		"nv-med.com","nybooks.com","observateurocde.org","oecd-ilibrary.org",
		"oecdobserver.org","oed.com","ojs.aaai.org","oldcitypublishing.com",
		"online.sagepub.com","onlinelibrary.wiley.com","open.library.ubc.ca","openaccess.thecvf.com",
		"openreview.net","ophthalmologytimes.modernmedicine.com","opticsinfobase.org","or.journal.informs.org",
		"orgsci.journal.informs.org","osa-opn.org","oup.com","ovidsp.ovid.com",
		"oxfordjournals.org","oxfordlanguagedictionaries.com","oxfordmusiconline.com","pacificarchaeology.org",
		"pads.dukejournals.org","palgrave-journals.com","paperpile.com","papers.nber.org",
		"pasj.asj.or.jp","peanutscience.com","pedagogy.dukejournals.org","peerj.com",
		"perceptionweb.com","pgrsa.org","pharmacists.ca","pharmrev.aspetjournals.org",
		"philreview.dukejournals.org","phycologia.org","physicsweb.org","physicsworldarchive.iop.org",
		"physiolgenomics.physiology.org","physiology.org","physrev.physiology.org","plantcell.org",
		"plantmanagementnetwork.org","plantphysiol.org","pld.chadwyck.com","plos.org",
		"plosjournals.org","plosone.org","pnas.org","podiatrytoday.com",
		"poeticstoday.dukejournals.org","polymersdatabase.com","portal.acm.org","portal.euromonitor.com",
		"portico.org","positions.dukejournals.org","pracademics.com","priory.com",
		"prisma.chadwyck.com","proceedings.mlr.press","proceedings.neurips.cc","products.asminternational.org",
		"projecteuclid.org","proquest.com","proquest.safaribooksonline.com","proquest.umi.com",
		"proxying.lib.ncsu.edu","ps.fass.org","psycnet-apa-org","psycnet.apa.org",
		"ptp.ipap.jp","publicculture.dukejournals.org","publish.csiro.au","publish.kne-publishing.com",
		"pubmed.cn","pubmed.com","pubmedcentral.nih.gov","pubmedcentralcanada.ca",
		"pubs.acs.org","pubs.acs.org.ccindex.cn","pubs.aic.ca","pubs.amstat.org",
		"pubs.rsc.org","pubservices.nrc-cnrc.ca","purl.access.gpo.gov","pwq.sagepub.com",
		"qjps.com","quod.lib.umich.edu","radiology.rsna.org","railwayage.com",
		"raj.sagepub.com","reading.org","redbooks.com","reference-global.com",
		"referenceusa.com","refuniv.odyssi.com","reproduction-online.org","researcherslinks.com",
		"researchgate.net","revista-iberoamericana.pitt.edu","revophth.com","rff.org",
		"rhr.dukejournals.org","rnajournal.cshlp.org","rnd.edpsciences.org","ropercenter.uconn.edu",
		"rothamsted.bbsrc.ac.uk","royalsociety.org.nz","royalsocietypublishing.org","rphr.endojournals.org",
		"rsc.org","rsh.sagepub.com","sagamorepub.com","sanborn.umi.com",
		"saq.dukejournals.org","sbrnet.com","schattauer.de","scholar.google.com",
		"sci-hub.bz","sci-hub.cc","sci-hub.ee","sci-hub.hk",
		"sci-hub.io","sci-hub.is","sci-hub.la","sci-hub.mu",
		"sci-hub.org","sci-hub.se","sci-hub.st","sci-hub.tv",
		"sci-hub.tw","sci-hub.win","science.sciencemag.org","sciencedirect.com",
		"sciencemag.org","scientific.net","seab.envmed.rochester.edu","search.ebscohost.com",
		"search.epnet.com","search.marquiswhoswho.com","search.proquest.com","search.rdsinc.com",
		"searchcenter.intelecomonline.net","seg.org","services.bepress.com","simplymap.com",
		"site.ebrary.com","slac.stanford.edu","social.chass.ncsu.edu","socialtext.dukejournals.org",
		"societyforchaostheory.org","spie.org","spiedl.org","springer.com",
		"springerlink.com","springerlink.de","springerlink.metapress.com","springerprotocols.com",
		"ssh.dukejournals.org","ssrn.com","stacks.iop.org","statpak.gov.pk",
		"stepsheet.stsci.edu","stke.sciencemag.org","studenttheses.uu.nl","swissmedic.ch",
		"symposium.cshlp.org","tandfonline.com","tannerlectures.utah.edu","tappi.micronexx.com",
		"taw.sagepub.com","tcsae.org","technologyreview.com","theannals.com",
		"theater.dukejournals.org","thecochranelibrary.com","theiwrc.org","thejns.org",
		"thelancet.com","themerckindex.cambridgesoft.com","theses.com","theses.hal.science",
		"thomist.org","toxnet.nlm.nih.gov","transci.journal.informs.org","trb.org",
		"turf.lib.msu.edu","turpion.org","tvnews.vanderbilt.edu","uark.edu",
		"ui.adsabs.harvard.edu","uli.org","ulrichsweb.com","unesp.br",
		"unstats.un.org","vdi.sagepub.com","veterinaryrecord.bmj.com","vetres.org",
		"vha.usc.edu","victoriandatabase.com","victorianperiodicals.com","vir.sgmjournals.org",
		"vnweb.hwwilsonweb.com","wanfangdata.com.cn","web.jbjs.org.uk","web.lexis-nexis.com",
		"webofknowledge.com","webthesis.biblio.polito.it","wgsn.com","whiv.alexanderstreet.com",
		"wikipedia.org","wiley.com","wilsonweb2.hwwilson.com","wkap.nl",
		"worldscientific.com","worldscinet.com","worldscinetarchives.com","worldshakesbib.org",
		"wrds-web.wharton.upenn.edu","wssa.allenpress.com","wto.org","www-pub.iaea.org",
		"www.ajas.info","www.biologicalpsychiatryjournal.com","www.biorxiv.org","www.ejog.org",
		"www.igi-global.com","www.ingentaconnect.com","www.koreascience.or.kr","www.mdpi.com",
		"www.nrcresearchpress.com","www.osapublishing.org","www.prophy.science","www.researchgate.net",
		"www.researchsquare.com","www.scitation.org","www.semanticscholar.org","www.taylorfrancis.com",
		"www.worldscientific.com","www.x-mol.com","www2.acs.ncsu.edu","www3.interscience.wiley.com",
		"www3.nationaljournal.com","www3.stat.sinica.edu.tw","xlink.rsc.org","xueshu.baidu.com",
		"ybook.co.jp","zenodo.org","zentralblatt-math.org","zhuanlan.zhihu.com",
		"znaturforsch.com",
	];
	// Extra rules preserved from the old @match block:
	//   *://*techrxiv*            -> any host containing "techrxiv"
	//   http(s)://*/doi/abs/*     -> any host, path under /doi/abs/
	//   http(s)://*/doi/full/*    -> any host, path under /doi/full/
	const _host = location.hostname.toLowerCase();
	const _path = location.pathname.toLowerCase();
	const _allowed =
		_host.indexOf("techrxiv") !== -1 ||
		_path.indexOf("/doi/abs/") !== -1 ||
		_path.indexOf("/doi/full/") !== -1 ||
		DOI_ALLOWED_DOMAINS.some(function (d) {
			return _host === d || _host.endsWith("." + d);
		});
	if (!_allowed) { return; }


/* ===========================================================================
 * DOI Button — site-agnostic core
 *
 * On any allowed page (the @match allow-list above just controls WHERE this
 * runs) this does four things, all derived from the DOI + title — no
 * per-publisher link rewriting:
 *   1. find the article's main title,
 *   2. find its DOI if present on the page,
 *   3. show floating Sci-Hub + WSU proxy buttons for that DOI,
 *   4. ask the local PRMA server if it's already saved and color the title
 *      green (saved) / orange (not saved), plus a floating status badge.
 *
 * The only site-specific bits are small DOI-location hints (Springer citation
 * line, IEEE DOI box) and a short skip-list for aggregator/search pages where
 * "the article" doesn't apply.
 * ======================================================================== */

"use strict";

/* ---------------- CONFIG ---------------- */
const SCIHUB_BASE = "https://sci-hub.st/";
const WSU_PROXY   = "https://doi-org.ezproxy.libraries.wright.edu/";          // DOI form
const WSU_LOGIN   = "https://login.ezproxy.libraries.wright.edu/login?url=";  // raw-URL form
const PRMA_API    = "http://127.0.0.1:9371/exists";

/* ---------------- ICONS (base64, verbatim) ---------------- */
var sci_hub_ico = "data:image/x-icon;base64,AAABAAcAMDAAAAEACACoDgAAdgAAACAgAAABAAgAqAgAAB4PAAAQEAAAAQAIAGgFAADGFwAAAAAAAAEAIABkegAALh0AADAwAAABACAAqCUAAJKXAAAgIAAAAQAgAKgQAAA6vQAAEBAAAAEAIABoBAAA4s0AACgAAAAwAAAAYAAAAAEACAAAAAAAAAkAAAAAAAAAAAAAAAEAAAABAAAAAAAAACrIAAAuygAAMcoAAzTLAAQ1ywAAMswAATXMAAU2zAACOc0ABDnNAAU9zwAJOc0ADDvOABA+zgAFP9AAEkHPABVCzwAXRM8ABUHQAAZF0QAIRdEAB0nTAAhJ0wAIS9QACU3UABtH0QAKUNUADFPWAAxV1gAMVtgADVnYAA5d2QAQXtoAIk7SACZQ0gAtV9UAMFjUADRb1QAPYNoAEGHaABFl3AASad0AFGjdABNs3QAUbt8AFXDfAChm2gA9ZNgAFXHgABZ14QAXeeIAGHviABh94gAef+MAGX3kAEFn2QBJbdoATnDaAFBy2wBXeN0AXn3eABqB5QAcguQAHIXmAB2I5wAdiugAHo3pAB+Q6gAgkuoAIJTrACGW7AAhmewAI5ztACSf7gA0nusAMKbuACSh8AAmpfAAJ6nyACiq8gAorfIAKa70ACmx8wAqsvUAK7X2ACy39QAsufYALLr4AC69+AAxvPcAZILfAEuE4gBcg+AAaYbgAHmT4wB7lOQAL8H5ADDD+gAwxfoAMMb8ADHI+wAyyv0ANMv+ADLM/gA1zP4AOs3+AD3N/gA00P4AQc7+AEPQ/wBF0P8ASdD+AE3R/gBR0v4AVtT+AFrV/gBe1v4AYtf+AG/U+QBj2P8Aatn+AG/b/wBx2/4Ac9z/AHXc/gB53f4Aft7+AIPe/QCF4P4AiOD+AI3i/gCR4/4AluT+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIR+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB+a2lrAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAdGlpaWlphAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgm5paWlpaWlpcQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHpraWlpaWlpaWlpaQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB0aWlpaWlpaWlpaWlpaXYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACEbmlpaWlpaWlpaWlpaWlpaWsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAemtpaWlpaWlpaWlpaWlpaWlpaWl+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHRpaWlpaWlpaWlpaWlpaWlpaWlpaWlrAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH9taWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlphAAA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AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADPM/w4zzP9OM8z/rjLL/vAzzP/9M8z/9DLL/i4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAyy/4ZMsv+bDLL/swyy/75Msv+/jLL/v4yy/7+Msv+/jLL/owyy/4BAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAzzP8FMsv+NzPM/5YzzP/qMsv+/jPM//8zzP//M8z//zLL/v4zzP//M8z//zLL/t4zzP8aAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMsv+DTPM/1AzzP+zMsv+8TPM//4zzP//Msv+/jPM//8zzP//M8z//zLL/v4zzP//M8z//zLL/vszzP9oAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADLL/hoyy/5wMsv+zDLL/vgyy/7+Msv+/jLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/v4yy/7DMsv+CgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADPM/wgzzP84Msv+lDPM/+gzzP/+Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//M8z//zLL/v4zzP//M8z//zLL/v4zzP/1M8z/SAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADPM/wEzzP8TMsv+VjPM/7kzzP/vMsv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//M8z//zLL/v4zzP//M8z//zLL/v4zzP/+M8z/rTLL/gMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAzzP8BMsv+HjPM/3szzP/QMsv++jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//M8z//zLL/v4zzP//M8z//zLL/v4zzP//M8z/9DLL/igAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMsv+BTLL/j4yy/6gMsv+7TLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/ocyy/4CAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADPM/xQzzP9ZM8z/uDLL/vIzzP/+Msv+/jPM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/v4yy/7yMsv+ujLL/lYyy/4OAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMsv+ATPM/8IzzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//M8z//zLL/v4zzP/8M8z/5TLL/pczzP85M8z/CgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADPM/1szzP/7Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//M8z/+zLL/tAzzP93M8z/JDLL/gIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADLL/hMyy/7YMsv+/jLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+/jLL/u4yy/6tMsv+SzLL/goAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAzzP9/Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//wzzP/fMsv+jjPM/zAzzP8GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAzzP8qMsv+7TPM//8zzP//Msv+/jPM//8zzP//Msv+/jPM//8zzP/9Msv+0TPM/2szzP8dMsv+AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAyy/4EMsv+pjLL/v4yy/7+Msv+/jLL/v4yy/7+Msv+7zLL/qsyy/5LMsv+CQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMsv+QzPM//0zzP//Msv+/DPM/90zzP+KMsv+MDPM/wUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMsv+BjPM/8wzzP/NMsv+azPM/xcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADLL/hcyy/4EAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP///////wAA////////AAD///////8AAP////+H/wAA/////Af/AAD////wA/8AAP///4AD/wAA///+AAP/AAD///gAAf8AAP//wAAB/wAA//4AAAD/AAD/+AAAAP8AAP/gAAAAfwAA/4AAAAB/AAD+AAAAAH8AAPgAAAAAPwAA/AAAAAA/AAA8AAAAAD8AAAAAAAAAHwAAAAAAAAAfAAAAAAAAAA8AABgAAAAADwAAAAAAAAAHAAAAAAAAAAcAAAAAAAAABwAAAAAAAAADAAAAAAAAAAMAAADAAAAAAQAAw8AAAAABAAD/4AAAAAAAAP/wAAAAAAAA//AAAAAAAAD/8AAAAAAAAP/4AAAAAAAA//gAAAADAAD/+AAAAA8AAP/8AAAAfwAA//wAAAH/AAD//gAAB/8AAP/+AAA//wAA//8AAP//AAD//wAD//8AAP//AB///wAA//+Af///AAD//4P///8AAP//z////wAA////////AAD///////8AACgAAAAgAAAAQAAAAAEAIAAAAAAAgBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMsz/JDLM/4IyzP+zMsz/DQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAyzP8DMsz/QjLM/6AyzP/tMsz//zLM//8yzP9SAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAyzP8SMsz/YzLM/8EyzP/+Msz//zLM//8yzP//Msz//zLM/7MAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAyzP8oMsz/gTLM/9oyzP//Msz//zLM//8yzP//Msz//zLM//8yzP//Msz/9zLM/zAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADLM/wYyzP9EMsz/ojLM/+8yzP//Msz//zLM//8yzP//Msz//zLM//8yzP//Msz//zLM//8yzP//Msz/kQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADLM/xYyzP9mMsz/xDLM//8yzP//Msz//zLM//8yzP//Msz//zLM//8yzP//Msz//zLM//8yzP//Msz//zLM//8yzP/pMsz/GgAAAAAAAAAAAAAAAAAA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";
var wsu_ico = "data:image/x-icon;base64,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";

/* ---------------- UTILITIES ---------------- */
function norm(s){
  return (s||"")
    .replace(/[\u200B-\u200D\uFEFF]/g,"")
    .replace(/\s+/g," ")
    .trim()
    .toLowerCase();
}
function visible(el){
  if(!el || !el.offsetParent) return false;
  const s=getComputedStyle(el);
  return s.display!=="none" && s.visibility!=="hidden";
}

function isBlacklisted() {
  return DOMAIN_BLACKLIST.some(entry => {
    const slash = entry.indexOf("/");
    const domain = slash === -1 ? entry : entry.slice(0, slash);
    const path = slash === -1 ? "" : entry.slice(slash); // e.g. "/maps"

    const domainMatch =
      location.hostname === domain || location.hostname.endsWith("." + domain);
    if (!domainMatch) return false;

    if (!path) return true;
    return location.pathname === path || location.pathname.startsWith(path + "/");
  });
}
/* ---------------- DOI DETECTION ----------------
 * Generic everywhere, with two publisher hints for pages that don't expose a
 * standard citation_doi meta tag. */
const DOI_RE = /\b10\.\d{4,9}\/[-._;()\/:A-Z0-9]+\b/i;

// Normalize a DOI before building proxy URLs. DOIs legitimately contain "." and
// "/", so this is conservative: it strips query/fragment, trailing punctuation,
// and well-known publisher "view" suffixes (.abstract, /meta, .full.pdf, ...) —
// but ONLY if what remains is still a structurally valid DOI. Otherwise it
// leaves the string untouched (DOIs are weird; better to under-clean).
//   10.1101/2021.05.23.445249.abstract -> 10.1101/2021.05.23.445249
//   10.1088/1741-2552/aae4b9/meta       -> 10.1088/1741-2552/aae4b9
const DOI_VIEW_SUFFIX =
  /(?:[./](?:abstract|full(?:[-.]?text)?|pdf|pdfplus|epub|html?|meta|short|long|summary|figures?|tables?|supplementary(?:-information)?))+$/i;

function cleanDoi(doi){
  if(!doi) return doi;
  let d = String(doi).trim().split(/[?#]/)[0];          // drop query + fragment
  // strip trailing . , ; only — NOT ) or ], which are valid DOI chars
  // e.g. 10.1130/2017.2526(03)
  d = d.replace(/[.,;]+$/, "");
  const stripped = d.replace(DOI_VIEW_SUFFIX, "").replace(/[.,;]+$/, "");
  return /^10\.\d{4,9}\/\S+$/.test(stripped) ? stripped : d;  // keep only if still a DOI
}

function doiFromMeta(){
  const m = document.querySelector(
    'meta[name="citation_doi"], meta[name="dc.identifier"], meta[name="DC.identifier"], meta[name="prism.doi"]'
  );
  const x = m && m.content && m.content.match(DOI_RE);
  return x ? x[0] : null;
}
function doiFromSpringer(){            // Springer Nature bibliographic line
  const cite = document.querySelector("p.c-bibliographic-information__citation");
  const x = cite && (cite.innerText || cite.textContent || "").match(DOI_RE);
  return x ? x[0] : null;
}
function doiFromIEEE(){                // IEEE Xplore DOI container
  const el = document.querySelector(
    'div[data-analytics_identifier="document_abstract_doi"] a[href*="doi.org"]'
  );
  const x = el && el.href.match(DOI_RE);
  return x ? x[0] : null;
}
function doiFromLinks(){               // any doi.org link on the page
  const a = document.querySelector('a[href*="doi.org/10."]');
  if(!a) return null;
  let href; try { href = decodeURIComponent(a.href); } catch(e){ href = a.href; }
  const x = href.match(DOI_RE);
  return x ? x[0] : null;
}
function doiMostFrequent(){            // fallback: most-repeated DOI in body text
  const re = /\b10\.\d{4,9}\/[-._;()\/:a-z0-9]+\b/ig;
  const text = document.body ? document.body.innerText : "";
  const seen = {};
  let m, best = null, bestN = 0;
  while((m = re.exec(text))){
    const d = m[0].replace(/[.,;]+$/,"");
    seen[d] = (seen[d]||0)+1;
    if(seen[d] > bestN){ bestN = seen[d]; best = d; }
  }
  return best;
}
function findDoi(){
  try {
    const d = doiFromMeta() || doiFromSpringer() || doiFromIEEE()
           || doiFromLinks() || doiMostFrequent() || null;
    return d ? cleanDoi(d) : null;
  } catch(e){ return null; }
}

/* ---------------- TITLE DETECTION ---------------- */
function detectTitle(){
  // 1. metadata
  const meta =
    document.querySelector('meta[name="citation_title"]') ||
    document.querySelector('meta[property="og:title"]') ||
    document.querySelector('meta[name="dc.title"]');
  if(meta && meta.content) return meta.content.trim();

  // 2. biggest real heading
  const headings = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6,[role='heading']"));
  if(headings.length){
    const best = headings.map(el=>{
      const size = parseFloat(getComputedStyle(el).fontSize) || 0;
      const text = el.innerText.trim();
      if(text.length < 8) return null;
      return { text, score: size * Math.log(text.length + 1) };
    }).filter(Boolean).sort((a,b)=>b.score-a.score)[0];
    if(best) return best.text;
  }

  // 3. biggest prominent text near the top
  let best = null;
  for(const el of Array.from(document.querySelectorAll("body *")).slice(0,150)){
    const text = el.innerText && el.innerText.trim();
    if(!text || text.length < 10 || text.length > 200) continue;
    const size = parseFloat(getComputedStyle(el).fontSize) || 0;
    if(size < 18) continue;
    const score = size * Math.log(text.length + 1);
    if(!best || score > best.score) best = { text, score };
  }
  if(best) return best.text;

  // 4. last resort
  return document.title.trim();
}
function findTitleElements(title){
  const target = norm(title);
  if(!target) return [];
  const out = [];
  for(const el of document.querySelectorAll("h1,h2,h3,span,p,div")){
    if(!visible(el)) continue;
    if(norm(el.innerText) === target) out.push(el);
  }
  return out;
}

/* ---------------- FLOATING PROXY BUTTONS (top-right, for the MAIN paper) --- */
function addFloatButton(id, rightPx, href, icon){
  let box = document.getElementById(id);
  if(!box){
    box = document.createElement("div");
    box.id = id;
    box.style.position = "fixed";
    box.style.right = rightPx;
    box.style.top = "100px";
    box.style.zIndex = "999999";
    document.body.appendChild(box);
  }
  box.innerHTML =
    `<a target="_blank" href="${href}">` +
      `<img src="${icon}" style="height:30px;width:30px;margin:2px;display:inline-block;">` +
    `</a>`;
}
function showProxyButtons(doi){
  addFloatButton("sciHubButton", "52px", SCIHUB_BASE + doi, sci_hub_ico);
  addFloatButton("wsuButton",    "12px", WSU_PROXY  + doi, wsu_ico);
}

/* ---------------- PER-LINK INLINE ICONS ----------------
 * Walk every <a>, pull a DOI / PMID / PMCID out of its href (the identifiers
 * the proxies accept), and drop small Sci-Hub (+ WSU for DOIs) icons next to
 * it. This is what makes listing pages useful — one icon per result, e.g. each
 * hit on a PubMed or Google Scholar results page, or each entry in a reference
 * list. A link qualifies if we can pull a DOI / PMID / PMCID from it, OR it
 * points at a publisher we already trust (host in DOI_ALLOWED_DOMAINS) — the
 * latter covers sites like IEEE that hide the DOI behind an internal id. */

// A DOI embedded anywhere in a URL (stops at query/fragment/separator chars).
const HREF_DOI_RE = /10\.\d{4,9}\/[^\s"'<>?#&]+/i;

function refFromHref(rawHref){
  let url;
  try { url = decodeURIComponent(rawHref); } catch(e){ url = rawHref; }

  // DOI — doi.org/…, /doi/abs|full|pdf/…, ?doi=…, access_num=10.…, bare 10.x/…
  let m = url.match(HREF_DOI_RE);
  if(m) return { doi: cleanDoi(m[0]) };

  // PMID — pubmed hosts, ?pmid=/list_uids=, or access_num=<digits>&link_type=MED
  m = url.match(/(?:pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov\/pubmed|pubmed\.com|pubmed\.cn|europepmc\.org\/abstract\/med)\/(\d{4,})/i)
   || url.match(/[?&](?:pmid|list_uids)=(\d{4,})/i)
   || (/link_type=MED/i.test(url) && url.match(/access_num=(\d{4,})/i));
  if(m) return { pmid: m[1] };

  // PMCID
  m = url.match(/\b(PMC\d{4,})\b/i);
  if(m) return { pmcid: m[1].toUpperCase() };

  // No identifier in the URL, but if the link points at a publisher we already
  // know (host in DOI_ALLOWED_DOMAINS, e.g. IEEE Xplore which only exposes an
  // internal document id) AND the path looks like it carries an article id, then
  // the link itself IS the article — proxy it whole. The id-like-path gate is
  // what keeps nav / footer / homepage links (/booklistinfo/home, /conference-
  // series, /) from getting icons: those have no id-bearing segment.
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    const known = DOI_ALLOWED_DOMAINS.some(d => host === d || host.endsWith("." + d));
    if(known && !isAggregatorHost(host) && looksLikeArticlePath(u.pathname)){
      // drop query + fragment: the path identifies the article, and a raw query
      // would break the unencoded login?url= concatenation (its & gets swallowed)
      return { url: url.split(/[?#]/)[0] };
    }
  } catch(e){}

  return null;
}

// A no-DOI article URL still carries an id-like path segment (IEEE 11505157,
// arXiv 2301.12345, …): long-ish and containing a digit. Nav / footer / home
// links (/booklistinfo/home, /conference-series, /) have none, so they're out.
function looksLikeArticlePath(pathname){
  return pathname.split("/").some(seg => seg.length >= 6 && /\d/.test(seg));
}

// Some allow-listed hosts are search / aggregator / social sites (the script
// runs there, but their internal links are NOT proxy-able articles). Exclude
// them from the raw-URL fallback above. (escholarship.org, googleapis PDF
// storage, etc. are real article hosts and intentionally NOT listed here.)
function isAggregatorHost(host){
  return /(^|\.)(google\.[a-z.]+|wikipedia\.org|baidu\.com|zhihu\.com|researchgate\.net|semanticscholar\.org)$/i.test(host);
}

// Sci-Hub from a DOI or a raw publisher article URL. NOTE: PMID/PMCID get NO
// icon — PubMed/PMC are free NIH resources (a PMCID is free full text), so
// proxying them is pointless. We still detect them (so a PubMed link is
// recognized as a paper and doesn't fall through to the raw-URL branch), they
// just produce no proxy link.
function sciHubUrlFor(ref){
  if(ref.doi)   return SCIHUB_BASE + ref.doi;
  if(ref.url)   return SCIHUB_BASE + ref.url;
  return null;
}

// WSU proxy: the doi.org-via-EZproxy form for DOIs; the login?url= form for a
// raw article URL (EZproxy can't host-rewrite a non-DOI cleanly). PMID/PMCID
// have no DOI and no plain article URL, so there's no WSU link for those.
function wsuUrlFor(ref){
  if(ref.doi) return WSU_PROXY + ref.doi;
  if(ref.url) return WSU_LOGIN + ref.url;
  return null;
}

// Does this link point back at the article we're already on? True when it's the
// same host and the same path OR a path nested under it (ignoring query /
// #fragment / trailing slash). Catches the outline anchors (Highlights, Abstract,
// …) AND same-article variants like the "View PDF" link
// (/pii/<id>/pdfft), figures, etc. — all of which would otherwise get a useless
// icon that just links back to this same article.
function isSelfLink(href){
  try {
    const a = new URL(href, location.href);
    const b = new URL(location.href);
    if(a.hostname.toLowerCase() !== b.hostname.toLowerCase()) return false;
    const pa = a.pathname.replace(/\/+$/, "").toLowerCase();
    const pb = b.pathname.replace(/\/+$/, "").toLowerCase();
    if(pa === pb) return true;
    // "nested under" only when the base path isn't root — on a search page the
    // path is just "/" (term is in the query), and pb+"/" === "/" would wrongly
    // match every absolute link (e.g. PubMed result /42294101/).
    return pb !== "" && pa.startsWith(pb + "/");
  } catch(e){ return false; }
}

// Insert a small icon link right after `refNode`; returns the inserted node.
function insertIconAfter(refNode, href, icon, kind){
  const a = document.createElement("a");
  a.href = href;
  a.target = "_blank";
  a.dataset.doiInlineIcon = kind;
  const img = document.createElement("img");
  img.src = icon;
  img.style.cssText = "height:15px !important;width:15px !important;margin:2px !important;display:inline-block;vertical-align:middle;";
  a.appendChild(img);
  refNode.parentNode.insertBefore(a, refNode.nextSibling);
  return a;
}

// Downloadable assets are never papers — don't decorate image/media/style links
// (e.g. ScienceDirect's "Download high-res image" graphical-abstract .jpg).
const ASSET_RE = /\.(jpe?g|png|gif|svg|webp|bmp|ico|tiff?|mp4|webm|mov|avi|mp3|wav|css|js|json|xml|woff2?|ttf|eot|zip|gz)$/i;

// "id-like" path segments of THIS article taken from the current URL — pii
// (S2666053925001249), IEEE doc id (11505157), arXiv (2301.12345), Nature
// (s41586-024-01234-5), etc. Used to suppress same-article ASSET / variant links
// (its figures, PDF, graphical-abstract image on a CDN host). NOTE: the article's
// own DOI is deliberately NOT included — links to the main DOI SHOULD get inline
// icons too (alongside the top-right floating button), so you can proxy from any
// instance of it on the page.
function pageSelfIds(){
  const t = [];
  for(const seg of location.pathname.split("/")){
    let s; try { s = decodeURIComponent(seg); } catch(e){ s = seg; }
    if(s.length >= 8 && /\d/.test(s)) t.push(s.toLowerCase());
  }
  return t;
}

// Decorate links to OTHER papers AND any link to THIS page's main DOI (so the
// proxy options are reachable inline, not just from the floating button). We
// still skip images, and same-article asset/variant links (figures, View PDF,
// graphical-abstract image) via isSelfLink + the pii-style self-ids.
function decorateLinks(){
  const selfIds = pageSelfIds();

  for(const anchor of document.querySelectorAll("a[href]")){
    if(anchor.dataset.doiDecorated) continue;   // already handled this link
    if(anchor.dataset.doiInlineIcon) continue;  // one of our own icons
    anchor.dataset.doiDecorated = "1";

    const href = anchor.href;
    if(!href || isSelfLink(href)) continue;     // same host + same/nested path
    if(!/^https?:/i.test(href)) continue;       // skip mailto:/javascript:/# etc.
    if(href.startsWith(SCIHUB_BASE) || href.startsWith(WSU_PROXY) || href.startsWith(WSU_LOGIN)) continue;
    // Already an EZproxy link? It already takes you to the proxied copy, so a
    // proxy icon would be redundant — skip it.
    try { if(new URL(href).hostname.toLowerCase().endsWith("ezproxy.libraries.wright.edu")) continue; } catch(e){}

    const lower = href.toLowerCase();
    if(ASSET_RE.test(lower.split(/[?#]/)[0])) continue;        // image/media/asset
    if(selfIds.some(t => lower.includes(t))) continue;         // same-article asset/variant

    const ref = refFromHref(href);
    if(!ref) continue;                          // no DOI/PMID/PMCID and not a known host

    let after = anchor;
    const sci = sciHubUrlFor(ref);
    if(sci) after = insertIconAfter(after, sci, sci_hub_ico, "sci");
    const wsu = wsuUrlFor(ref);
    if(wsu) insertIconAfter(after, wsu, wsu_ico, "wsu");
  }
}

/* ---------------- PRMA SERVER + TITLE COLORING ---------------- */
const http = (typeof GM !== "undefined" && GM.xmlHttpRequest) ? GM.xmlHttpRequest : GM_xmlhttpRequest;

function askServer(title){
  return askServerBatch([title]);   // single title -> one-element batch
}

// Ask PRMA about many titles at once. Returns the parsed response; results[i]
// corresponds to titles[i].
function askServerBatch(titles){
  return new Promise((resolve, reject)=>{
    http({
      method: "POST",
      url: PRMA_API,
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({ queries: titles.map((t, i) => ({ id: String(i), title: t })) }),
      onload: r => { try { resolve(JSON.parse(r.responseText)); } catch(e){ reject(e); } },
      onerror: reject
    });
  });
}

(function injectCSS(){
  const style = document.createElement("style");
  // Highlighter-style background (NOT an outline): outlines + vertical padding on
  // a multi-line inline <a> draw box edges through the text and make titles
  // unreadable. Background-only + box-decoration-break:clone tints each wrapped
  // line cleanly behind the text, and 0 vertical padding avoids line overlap.
  style.textContent = `
  [data-zotero-exists]{
    border-radius:3px !important;
    padding:0 2px !important;
    -webkit-box-decoration-break:clone !important;
    box-decoration-break:clone !important;
  }
  [data-zotero-exists="true"]{ background:rgba(34,197,94,.32) !important; }
  [data-zotero-exists="false"]{ background:rgba(255,152,0,.32) !important; }`;
  document.documentElement.appendChild(style);
})();

// Floating badge guarantees the result is visible even if no title element
// could be matched/colored on a given page.
function showStatusBadge(exists){
  let box = document.getElementById("zoteroExistsBox");
  if(!box){
    box = document.createElement("div");
    box.id = "zoteroExistsBox";
    box.style.position = "fixed";
    box.style.right = "12px";
    box.style.top = "140px";
    box.style.zIndex = "999999";
    box.style.fontFamily = "system-ui,-apple-system,sans-serif";
    box.style.pointerEvents = "none";
    document.body.appendChild(box);
  }
  const color = exists ? "#22c55e" : "#ff9800";
  const text  = exists ? "Zotero \u2713" : "Not in Zotero";
  box.innerHTML =
    `<div style="background:${color};color:#fff;padding:8px 14px;border-radius:8px;` +
    `font-weight:600;font-size:14px;box-shadow:0 6px 18px rgba(0,0,0,.25);">${text}</div>`;
}

function colorTitle(title, exists){
  document.querySelectorAll("[data-zotero-exists]").forEach(e => e.removeAttribute("data-zotero-exists"));
  for(const el of findTitleElements(title)){
    el.setAttribute("data-zotero-exists", exists ? "true" : "false");
  }
  showStatusBadge(exists);
}

// Listing-page highlighter: color EVERY result by whether it's in Zotero. Each
// result's title is a long, multi-word link; we batch all of them to PRMA in one
// request and color each link green (saved) / orange (not saved). Re-runs (from
// the MutationObserver / infinite scroll) only touch links not yet checked.
async function evaluateListing(){
  const items = [];           // { el, title }
  const seen = new Set();
  for(const a of document.querySelectorAll("a[href]")){
    if(a.hasAttribute("data-zotero-exists")) continue;   // already colored
    if(!visible(a)) continue;
    const title = (a.innerText || "").trim();
    if(title.length < 25 || title.length > 320 || !/\s/.test(title)) continue;  // title-ish
    // Skip Scholar source links like "[PDF] worldscientific.com": drop any
    // leading [PDF]/[HTML]/[BOOK] tag, then reject anything that's just a
    // domain / URL (a real title has spaces, so it won't match this).
    const core = title.replace(/^\s*\[[^\]]*\]\s*/, "").trim();
    if(!core || /^\S+\.[a-z]{2,}(\/\S*)?$/i.test(core)) continue;
    // The result title is rendered larger than the small action links
    // ("Free Full Text from Publisher", "View Full Text on ProQuest", "Find It",
    // …). Require a title-sized font so we color the title and not those.
    if((parseFloat(getComputedStyle(a).fontSize) || 0) < 16) continue;
    const key = norm(title);
    if(!key || seen.has(key)) continue;
    seen.add(key);
    items.push({ el: a, title });
  }
  if(!items.length) return;
  try {
    const r = await askServerBatch(items.map(it => it.title));
    const res = (r && r.results) || [];
    items.forEach((it, i) => {
      const exists = res[i] && res[i].exists === true;
      it.el.setAttribute("data-zotero-exists", exists ? "true" : "false");
    });
  } catch(e){
    console.error("[doi-button] PRMA batch error:", e);
  }
}

/* ---------------- ORCHESTRATION ---------------- */
let lastTitle = null, debounceTimer = null;

// Search/reference pages where a "proxy this article" button is meaningless.
function skipButtons(){
  const h = location.hostname.toLowerCase();
  return h.includes("wikipedia.org") || h.includes("scholar.google");
}

// The per-<a> inline icons only make sense on a SEARCH / RESULTS / LISTING page
// (many different papers). On a single article page they just produce noise on
// self-references, nav, breadcrumbs, figure images, etc. — so we gate them here.
// Covers Google Scholar, PubMed, Web of Science summaries, arXiv/ScienceDirect/
// Springer/IEEE/Wiley/Nature/Cell/Zenodo search & doSearch pages (direct and
// EZproxy-wrapped), plus ANY url with a "search" action (but not "research").
function isListingPage(){
  const host = location.hostname.toLowerCase();
  if(host.includes("scholar.google")) return true;
  // PubMed: search/results pages are listings, but /<pmid>/ is a single article.
  if(host.includes("pubmed.ncbi.nlm.nih.gov")) return !/^\/\d+\/?$/.test(location.pathname);
  if(host.includes("webofscience") && location.href.toLowerCase().includes("summary")) return true;
  // generic "search"/"doSearch" action — strip "research" first so paths like
  // /research-article/ don't false-match.
  return location.href.toLowerCase().replace(/research/g, "").includes("search");
}

// Are we already viewing through the WSU EZproxy? If so we already have access,
// so inline icons on an article page are just noise. If NOT, we're on the raw
// publisher and the proxy icons are worth showing (incl. on article pages).
function onEzproxy(){
  return location.hostname.toLowerCase().endsWith("ezproxy.libraries.wright.edu");
}

// We only want the MAIN article page, never its children (figure / table /
// media / metrics sub-pages, e.g. /articles/<id>/figures/1). Two signals,
// host-agnostic so they survive the ezproxy URL rewrite (only the host
// changes; the path is preserved):
//   1. a canonical <link> points "up": the current path is strictly DEEPER
//      than the article's canonical path  ->  we're on a sub-page.
//   2. the path contains a known article sub-resource segment.
// Direct file resource (pdf, image, doc, video, etc.) rather than an HTML
// page — e.g. a publisher CDN figure URL. Nothing to decorate here.
function isFilePage(){
  // The browser hands raw files (images/pdfs/video) to a plugin/viewer; the
  // document body is then a single <img>/<embed>/<video> and not real HTML.
  const ct = (document.contentType || "").toLowerCase();
  if(ct && ct !== "text/html" && ct !== "application/xhtml+xml") return true;

  // Fallback: sniff the URL path's extension.
  let path;
  try { path = new URL(location.href).pathname; }
  catch(e){ path = location.pathname; }
  return /\.(pdf|jpe?g|png|gif|webp|bmp|svg|tiff?|ico|mp4|webm|mkv|avi|mov|wmv|flv|m4v|mp3|wav|flac|ogg|aac|m4a|zip|gz|tar|rar|7z|docx?|xlsx?|pptx?|csv|epub|mobi|ps|eps)$/i.test(path);
}

function isArticleChildPage(){
  const here = location.pathname.replace(/\/+$/,"").toLowerCase();

  const can = document.querySelector('link[rel="canonical"]');
  if(can && can.href){
    try {
      const cp = new URL(can.href).pathname.replace(/\/+$/,"").toLowerCase();
      // strictly deeper than the canonical article = a child page
      if(cp && here !== cp && here.startsWith(cp + "/")) return true;
    } catch(e){}
  }

  // fallback for pages without a useful canonical
  if(/\/(figures?|tables?|media|mediaobjects|supplementary-information|metrics)(\/|$)/i.test(here)) return true;

  return false;
}

async function run(){

  // if is blacklisted
  if ( isBlacklisted() ) return;

  // Direct file resource (pdf, image, doc, movie, etc.) — not an HTML page.
  if( isFilePage() ) return;

  // Only the main article — bail out on figure/table/etc. child pages.
  if( isArticleChildPage() ) return;

  // The page's own article DOI (null on listing pages). Used both for the
  // floating button and to suppress inline icons on self/chrome links.
  const mainDoi = findDoi();

  // Per-result inline icons — on search / listing pages (many papers), OR
  // whenever we're NOT already on EZproxy (raw publisher: the proxy options are
  // worth showing on the article + its references too). On an EZproxy article
  // page we skip them: you already have access, so they'd just be noise.
  if(isListingPage() || !onEzproxy()) decorateLinks();

  // (2)+(3) floating proxy buttons for the single main article
  if(!skipButtons() && mainDoi) showProxyButtons(mainDoi);

  // (1)+(4) PRMA title highlighting.
  // Listing page -> color EVERY result. Otherwise (article page) -> color this
  // one article's main title.
  if(isListingPage()){
    evaluateListing();
    return;
  }
  const title = detectTitle();
  if(!title) return;
  if(norm(title) === norm(lastTitle)) return;
  lastTitle = title;
  try {
    const r = await askServer(title);
    const exists = r && r.results && r.results[0] && r.results[0].exists === true;
    colorTitle(title, exists);
  } catch(e){
    console.error("[doi-button] PRMA server error:", e);
  }
}

function schedule(){
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(run, 700);
}

window.addEventListener("load", run);
new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });

})();