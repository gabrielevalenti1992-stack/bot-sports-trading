const { database } = require('/tmp/data.js');

// --- Esclusioni con motivazione verificata da ricerca web ---
const escluse = {
  6:"Trading Sportivo — Decreto Dignità: pubblicità giochi con vincite vietata (sanz. min 50.000€)",
  7:"idem",8:"idem",9:"idem",10:"idem",71:"idem",72:"idem",73:"idem",74:"idem",75:"idem",
  11:"Excel gratuiti dominanti: Fantamagazine, Consiglifantacalcio, ItaliaExcel",
  45:"Portale Offerte ARERA: ufficiale e gratuito, stesso identico output",
  87:"6+ calcolatori gratis italiani dominanti (Lodgify, CalcolaAffitti, AffittoNetto, TiCalcolo)",
  50:"Fiscozen/Quickfisco offrono il calcolo tasse forfettario come lead magnet gratuito",
  2:"Myfxbook, EarnForex, Dukascopy: calcolatori position size gratis e maturi",
  4:"Investing.com: calendario + notifiche push identiche, gratis, milioni di utenti",
  17:"JustWatch: aggregatore streaming ufficiale, gratis, presente in Italia",
  18:"Taranify, WhatWatch (IT), Limelight, Deabyday: quiz mood-based gratis dominanti",
  22:"TiCalcolo, FiscoeTasse Excel, Romano Automobili: calcolatori TCO gratis dominanti",
  24:"Thorpia: calcolatore gratuito ufficiale per tabelle millesimali",
  33:"RealOEM, ETKA, 7zap: cataloghi OEM gratuiti e ufficiali per marca",
  35:"laleggepertutti.it: fac-simile ricorso Prefetto/Giudice di Pace gratis",
  37:"Royal Canin, Purina Institute, Area-Dog: calcolatori RER gratis dei produttori",
  41:"Leroy Merlin, Klint, Capriolese: calcolatori vernice gratis dominanti",
  43:"Discogs: 500M+ transazioni, grading Goldmine ufficiale, è LO standard gratuito",
  46:"Jobscan, Novoresume, Enhancv, ResumeWorded: scanner ATS gratis maturi",
  47:"AppLI del Ministero del Lavoro: simulatore colloqui ufficiale e gratuito",
  48:"App 'Voice Post': nota vocale -> post LinkedIn, stessa funzione, gratis",
  49:"NotebookLM (Google): riassume libri interi, mappe concettuali, gratis illimitato",
  52:"QuantoSpendo, Bobby, BillKiller: tracker abbonamenti gratis dominanti in IT",
  53:"LeggeInChiaro.it, LexDo.it: generatore NDA italiano gratis, stesse clausole",
  56:"CataSync, Hypotenuse, Pixelfox: generatori schede prodotto AI gratis",
  58:"ListingGenie, Vinkit, SharkScribe, AutoLister: generatori titoli gratis dedicati",
  60:"CheckLocalSEO, LocalSEOAuditTool, Semrush: audit locali gratis in 60 secondi",
  63:"Decine di siti di conversione/sostituzione ingredienti gratis consolidati",
  65:"Bonebenders (IT, no registrazione), Codifa, Patient.info: checker gratis",
  77:"Prospre, Arvo, IronManager Academy: calcolatori macro gratis in italiano",
  79:"TrainerRoad, Ventro Cycling, Sport-Calculator: calcolatori FTP gratis",
  95:"Quickfisco, Assocaaf, Studio Campesato: checklist 730 gratis scaricabili",
  98:"investitorecomune.it, MiaCalcolatrice, Cernarus: calcolatori fondo emergenza gratis",
  16:"animefillerlist.com: standard globale gratuito, database enorme, app dedicata",
  42:"Tomappo, Calendario Semine, Giardino Verde: app gratis con meteo e fasi lunari",
  78:"Fantamagazine ha gia un tool Mantra con algoritmo intelligente, stesso spazio di #11"
};

const restanti = database.filter(d => !escluse[d.id]);
console.log("Esclusi:", Object.keys(escluse).length, " | Restanti:", restanti.length, "\n");

// --- Punteggio di qualita per i restanti (stesso schema della sessione precedente) ---
const legalByCat = {
  "Trading Finanziario": 3, "Finanza & Legal": 2, "Salute & Benessere": 2, "Pet Care": 3,
  "Affitti & Noleggi": 4, "Auto & Motori": 4, "Vivere da Soli": 5, "Calcio & Sport": 5,
  "Cinema & Anime": 4, "Casa & Hobby": 5, "Produttività & Lavoro": 5, "Digital Marketing": 5
};
const heavy = /\bbot\b|alert|notific|tempo reale|monitora|aggregat|estension|web-?app|api|scraper|scanner|algoritmo che|indicatore grafico|script che/i;
const light = /excel|foglio|template|tabella|check-?list|guida|modulo|pdf|planner|calendario|scheda|dashboard|calcolat|manuale|vademecum|protocollo|roadmap|lista|registro|sheet|verbale|kit|diffida/i;
const chan = s => /TikTok|IG|Instagram|FB|Facebook/i.test(s) ? 5 : /Forum|Telegram|Discord|LinkedIn|Glassdoor/i.test(s) ? 4 : /Reddit|YouTube/i.test(s) ? 3 : 3;
const stag = {23:5,26:5,29:5,91:4,92:5,93:5,95:4,98:4,99:4,78:5,14:3,80:3,44:2,88:2,36:2};

const rows = restanti.map(it => {
  const txt = it.title + " " + it.solution;
  let T = 3; if (light.test(txt) && !heavy.test(txt)) T = 5; if (heavy.test(txt)) T = 2;
  let A = 5; if (heavy.test(txt)) A = 3; if (/bot|alert|notific|tempo reale/i.test(txt)) A = 2;
  let C = 5; if (heavy.test(txt)) C = 3;
  const L = legalByCat[it.cat] ?? 4;
  const X = chan(it.source);
  const S = stag[it.id] ?? 4;
  const D = it.demandScore / 20;
  const P = Math.min(5, it.priceVal / 4);
  const score = T*3.0 + A*2.5 + C*1.5 + L*2.0 + X*2.0 + S*1.5 + D*1.5 + P*1.0;
  return { id: it.id, t: it.title, cat: it.cat, score: +score.toFixed(1) };
});
rows.sort((a,b) => b.score - a.score);

const TOP = rows.slice(0, 50);
const CUT = rows.slice(50);

console.log("=== TOP 50 (selezionate) ===");
TOP.forEach((r,i)=>console.log(String(i+1).padStart(2), String(r.score).padStart(5), r.cat.padEnd(22), '#'+r.id, r.t.slice(0,48)));

console.log("\n=== ULTIME 10 TAGLIATE PER PUNTEGGIO (di 60 restanti) ===");
CUT.forEach(r=>console.log(String(r.score).padStart(5), r.cat.padEnd(22), '#'+r.id, r.t.slice(0,50)));

require('fs').writeFileSync('escluse.json', JSON.stringify(escluse,null,1));
require('fs').writeFileSync('top50_ids.json', JSON.stringify(TOP.map(r=>r.id)));
require('fs').writeFileSync('cut10_ids.json', JSON.stringify(CUT.map(r=>r.id)));
console.log("\nTotale finale selezionato:", TOP.length);
