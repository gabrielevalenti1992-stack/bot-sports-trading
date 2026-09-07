const P = 9.99;                       // prezzo di vendita

// Fee piattaforma verificate (2026)
const piatt = {
  "Payhip (free)":   v => v*0.05 + (v*0.015 + 0.25),        // 5% + Stripe EU
  "Ko-fi (free)":    v => v*0.05 + (v*0.015 + 0.25),
  "Gumroad":         v => v*0.10 + 0.46 + (v*0.029 + 0.28), // 10% + 0,50$ + processing
  "Etsy":            v => 0.19 + v*0.065 + (v*0.04 + 0.30)  // listing + 6,5% + processing
};
console.log("NETTO PER VENDITA A " + P.toFixed(2) + " € (prima delle imposte)\n");
const netti = {};
for (const [n,f] of Object.entries(piatt)) {
  const fee = f(P); netti[n] = P - fee;
  console.log("  " + n.padEnd(18), "fee", fee.toFixed(2)+" €", " netto", (P-fee).toFixed(2)+" €",
              " (" + (fee/P*100).toFixed(1) + "%)");
}
const netto = netti["Payhip (free)"];

// Scenari fiscali (dati verificati 2026)
const sc = [
 { n:"A. Prestazione occasionale",
   fissi: 0,
   nota:"nessun costo fisso; limite 5.000 € lordi/anno; ritenuta 20% in acconto; l'attività deve restare non abituale e non organizzata",
   perVendita: v => 0 },
 { n:"B. P.IVA forfettaria professionale (gestione separata)",
   fissi: 365,
   nota:"commercialista online 365 €/anno (Quickfisco Standard); apertura 0 €; nessun contributo fisso; INPS 26,07% sul solo reddito; i contributi del 1° anno si versano l'anno dopo",
   perVendita: v => { const imp = P*0.78; const inps = imp*0.2607; return inps + (imp-inps)*0.05; } },
 { n:"C. Ditta individuale, commercio elettronico (ATECO 47.91.10)",
   fissi: 4611.64 + 449 + 88.50 + 120,
   nota:"INPS commercianti FISSI 4.611,64 €/anno anche a fatturato zero, + commercialista 449 + apertura 88,50 + diritto camerale e bolli ~120; richiede SCIA e Camera di Commercio",
   perVendita: v => { const imp = P*0.40; return imp*0.05; } }
];

console.log("\n\nQUANTE VENDITE PER ANDARE IN PARI (prezzo " + P + " €, canale Payhip)\n");
for (const s of sc) {
  const marg = netto - s.perVendita();
  const be = s.fissi === 0 ? 1 : Math.ceil(s.fissi / marg);
  console.log(s.n);
  console.log("   costi fissi anno 1: " + s.fissi.toFixed(2) + " €   margine per vendita: " + marg.toFixed(2) + " €");
  console.log("   PAREGGIO: " + be + " vendite" + (s.fissi===0 ? " (la prima vendita è già utile)" : ""));
  console.log("   " + s.nota + "\n");
}

console.log("UTILE NETTO A DIVERSI VOLUMI (scenario B)\n");
const B = sc[1], margB = netto - B.perVendita();
console.log("  vendite   incasso     utile netto");
[10,30,50,100,300,1000].forEach(v=>{
  const u = v*margB - B.fissi;
  console.log("  " + String(v).padStart(6), (v*P).toFixed(2).padStart(9)+" €",
              (u>=0?" ":"") + u.toFixed(2).padStart(10)+" €");
});
