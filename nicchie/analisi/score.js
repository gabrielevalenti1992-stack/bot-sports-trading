const {database, playbook} = require('/tmp/data.js');

// ---- Rischio legale / regolatorio per categoria (5 = nullo) ----
const legalByCat = {
  "Trading Sportivo": 1,       // gioco con vincite in denaro: divieto pubblicita (DL 87/2018)
  "Trading Finanziario": 3,    // niente consulenza; solo strumenti neutri
  "Finanza & Legal": 2,        // template legali = responsabilita professionale
  "Salute & Benessere": 2,     // claim salutistici
  "Pet Care": 3,
  "Affitti & Noleggi": 4,
  "Auto & Motori": 4,
  "Vivere da Soli": 5,
  "Calcio & Sport": 5,
  "Cinema & Anime": 4,         // possibili questioni di copyright sui cataloghi
  "Casa & Hobby": 5,
  "Produttività & Lavoro": 5,
  "Digital Marketing": 5
};

// ---- Segnali testuali su solution+title: infrastruttura vs file statico ----
const heavy = /\bbot\b|alert|notific|tempo reale|monitora|aggregat|estension|web-?app|api|scraper|scanner|algoritmo che|motore di ricerca|app\b|piattaform|indicatore grafico|script che/i;
const light = /excel|foglio|template|tabella|check-?list|guida|modulo|pdf|planner|calendario|scheda|dashboard|calcolat|manuale|vademecum|protocollo|roadmap|lista|registro|sheet/i;
const aiHeavy = /\bAI\b|intelligenza artificiale|genera(tore)? di (testi|report)|trasforma le specifiche|analizza il pdf|riassum|sintetizz|converte audio|note vocali/i;

// ---- Canale gratuito dove il pubblico e gia radunato e la vendita e tollerata ----
const chan = s => {
  if (/TikTok|IG|Instagram/i.test(s)) return 5;
  if (/FB|Facebook/i.test(s)) return 5;
  if (/Reddit/i.test(s)) return 3;      // autopromozione spesso vietata
  if (/Forum/i.test(s)) return 4;
  if (/Telegram|Discord/i.test(s)) return 4;
  if (/LinkedIn|Glassdoor|X\b|Twitter/i.test(s)) return 4;
  if (/YouTube/i.test(s)) return 3;
  return 3;
};

// ---- Stagionalita rispetto a OGGI: 7 settembre ----
const stag = {}; [11,78].forEach(i=>stag[i]=5);           // aste fantacalcio: finestra viva adesso
[95].forEach(i=>stag[i]=5);                                 // 730: scadenza 30 settembre
[26,92,29,93].forEach(i=>stag[i]=5);                        // trasloco/studenti/utenze: settembre e il picco
[91].forEach(i=>stag[i]=4);                                 // cambio armadio: ottobre
[42,80,14].forEach(i=>stag[i]=3);                           // stagione parzialmente passata
[21,25,36,88,44].forEach(i=>stag[i]=2);                     // picco estivo appena finito
[83,20,16,17,18,19,81,82,84,85].forEach(i=>stag[i]=4);      // intrattenimento: evergreen
[38].forEach(i=>stag[i]=2);                                 // botti: picco a dicembre

const rows = database.map(it => {
  const txt = it.title + " " + it.solution;
  const pb = playbook[it.id];

  // Tempo di realizzazione (5 = poche ore, 1 = settimane)
  let T = 3;
  if (light.test(txt) && !heavy.test(txt)) T = 5;
  if (heavy.test(txt)) T = 2;
  if (aiHeavy.test(txt)) T = Math.min(T, 2);
  if (/bot (telegram|whatsapp)|in tempo reale|scraper|monitora/i.test(txt)) T = 1;

  // Consegna automatica senza lavoro per vendita (5 = file scaricabile)
  let A = 5;
  if (heavy.test(txt)) A = 3;
  if (aiHeavy.test(txt)) A = 2;              // richiede elaborazione per cliente
  if (/su misura|personalizzat|generato su misura|report automatico/i.test(txt)) A = Math.min(A,2);
  if (/bot|alert|notific|tempo reale/i.test(txt)) A = 2;

  // Costo ricorrente di esercizio (5 = zero)
  let C = 5;
  if (heavy.test(txt)) C = 3;
  if (aiHeavy.test(txt)) C = 2;              // token AI a ogni consegna
  if (/api|bot|tempo reale|monitora|aggregat/i.test(txt)) C = 2;

  const L = legalByCat[it.cat] ?? 4;
  const X = chan(it.source);
  const S = stag[it.id] ?? 4;
  const D = it.demandScore / 20;                      // 0-5
  const P = Math.min(5, it.priceVal / 4);             // 0-5, ~20 euro = 5

  // Pesi: dominano velocita di realizzazione, assenza di attrito e canale
  const score = T*3.0 + A*2.5 + C*1.5 + L*2.0 + X*2.0 + S*1.5 + D*1.5 + P*1.0;
  return {id: it.id, t: it.title, cat: it.cat, prezzo: it.priceVal, T, A, C, L, X, S,
          D: +D.toFixed(1), P: +P.toFixed(1), score: +score.toFixed(1)};
});

rows.sort((a,b) => b.score - a.score);
console.log("TOP 20 — velocita di realizzazione e di incasso\n");
console.log("  # score  T A C L X S  prezzo  categoria                 titolo");
rows.slice(0,20).forEach((r,i)=>console.log(
  String(i+1).padStart(3), String(r.score).padStart(5),
  ` ${r.T} ${r.A} ${r.C} ${r.L} ${r.X} ${r.S}`,
  (r.prezzo+"€").padStart(7), " ", r.cat.padEnd(24), r.t.slice(0,52)));

console.log("\nULTIMI 8 (le piu lente / rischiose)\n");
rows.slice(-8).forEach(r=>console.log(String(r.score).padStart(5), ` ${r.T} ${r.A} ${r.C} ${r.L} ${r.X} ${r.S}`, " ", r.cat.padEnd(22), r.t.slice(0,50)));
require('fs').writeFileSync('classifica.json', JSON.stringify(rows,null,1));
console.log("\nLegenda: T=tempo build  A=consegna automatica  C=costo esercizio  L=rischio legale  X=canale  S=stagione oggi (5=meglio)");
