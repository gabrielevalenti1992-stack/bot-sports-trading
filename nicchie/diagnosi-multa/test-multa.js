const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' }).catch(()=>chromium.launch());
  const page = async () => {
    const p = await b.newPage({ viewport: { width: 420, height: 1600 } });
    const errs = [];
    p.on('pageerror', e => errs.push('PAGEERROR: ' + e.message));
    p.on('console', m => { if (m.type()==='error') errs.push('CONSOLE: ' + m.text()); });
    await p.goto('file:///home/user/bot-sports-trading/nicchie/diagnosi-multa/diagnosi-multa.html');
    p._errs = errs;
    return p;
  };
  let PASS=0, FAIL=0;
  const check = (label, cond, detail='') => { console.log((cond?'PASS':'FAIL'), label, detail); cond?PASS++:FAIL++; };

  // --- CASO 1: notifica tardiva, residente IT (100 giorni, limite 90) -> deve scattare vizio notifica ---
  {
    const p = await page();
    await p.fill('#d_inf', '2026-01-01');
    await p.fill('#d_not', '2026-04-11'); // 100 giorni dopo (verificato: 31+28+31+11=101, controllo sotto)
    await p.click('[data-v="si"][onclick*="residente"]');
    await p.click('[data-v="no"][onclick*="noleggio"]');
    await p.selectOption('#tipo_accertamento', 'immediata');
    await p.click('[data-v="si"][onclick*="firma"]');
    await p.click('[data-v="si"][onclick*="dati"]');
    await p.click('button:has-text("Genera la diagnosi")');
    await p.waitForTimeout(150);
    const testo = await p.locator('#risultato').innerText();
    check('Caso1: notifica tardiva rilevata', testo.includes('Notifica oltre i termini'), testo.slice(0,80));
    check('Caso1: nessun errore JS', p._errs.length === 0, p._errs.join('|'));
    await p.close();
  }

  // --- CASO 2: notifica in tempo (30 giorni, limite 90) -> NON deve scattare il vizio ---
  {
    const p = await page();
    await p.fill('#d_inf', '2026-01-01');
    await p.fill('#d_not', '2026-01-31'); // 30 giorni
    await p.click('[data-v="si"][onclick*="residente"]');
    await p.click('[data-v="no"][onclick*="noleggio"]');
    await p.selectOption('#tipo_accertamento', 'immediata');
    await p.click('[data-v="si"][onclick*="firma"]');
    await p.click('[data-v="si"][onclick*="dati"]');
    await p.click('button:has-text("Genera la diagnosi")');
    await p.waitForTimeout(150);
    const testo = await p.locator('#risultato').innerText();
    check('Caso2: NESSUN vizio di notifica (30gg < 90gg)', !testo.includes('Notifica oltre i termini'));
    check('Caso2: nessun vizio individuato quando tutto ok', testo.includes('Nessun vizio evidente'), testo.slice(0,100));
    await p.close();
  }

  // --- CASO 3: residente estero, 200 giorni (sotto 360) -> NON deve scattare; 400 giorni -> deve scattare ---
  {
    const p = await page();
    await p.fill('#d_inf', '2026-01-01');
    await p.fill('#d_not', '2026-07-20'); // ~200 giorni
    await p.click('[data-v="no"][onclick*="residente"]'); // residente estero
    await p.click('[data-v="no"][onclick*="noleggio"]');
    await p.selectOption('#tipo_accertamento', 'immediata');
    await p.click('[data-v="si"][onclick*="firma"]');
    await p.click('[data-v="si"][onclick*="dati"]');
    await p.click('button:has-text("Genera la diagnosi")');
    await p.waitForTimeout(150);
    let testo = await p.locator('#risultato').innerText();
    check('Caso3a: residente estero 200gg < 360gg -> nessun vizio notifica', !testo.includes('Notifica oltre i termini'));

    await p.fill('#d_not', '2027-02-15'); // oltre 360 giorni da 2026-01-01
    await p.click('button:has-text("Genera la diagnosi")');
    await p.waitForTimeout(150);
    testo = await p.locator('#risultato').innerText();
    check('Caso3b: residente estero oltre 360gg -> vizio notifica scatta', testo.includes('Notifica oltre i termini'));
    await p.close();
  }

  // --- CASO 4: autovelox senza omologazione + senza taratura -> 2 vizi ad alta forza ---
  {
    const p = await page();
    await p.fill('#d_inf', '2026-06-01');
    await p.fill('#d_not', '2026-06-20');
    await p.click('[data-v="si"][onclick*="residente"]');
    await p.click('[data-v="no"][onclick*="noleggio"]');
    await p.selectOption('#tipo_accertamento', 'autovelox');
    await p.waitForTimeout(100);
    await p.click('[data-v="no"][onclick*="omologazione"]');
    await p.click('[data-v="no"][onclick*="taratura"]');
    await p.click('[data-v="si"][onclick*="dati"]');
    await p.click('button:has-text("Genera la diagnosi")');
    await p.waitForTimeout(150);
    const testo = await p.locator('#risultato').innerText();
    check('Caso4: omologazione non provata rilevata', testo.includes('Omologazione dello strumento non provata'));
    check('Caso4: taratura non documentata rilevata', testo.includes('Taratura periodica non documentata'));
    check('Caso4: cita Cassazione 10505/2024', testo.includes('10505/2024'));
    const vizi = await p.locator('.vizio').count();
    check('Caso4: esattamente 2 vizi individuati', vizi === 2, 'trovati=' + vizi);
    await p.close();
  }

  // --- CASO 5: errore nei dati del verbale -> il testo inserito deve comparire nel ricorso generato ---
  {
    const p = await page();
    await p.fill('#d_inf', '2026-06-01');
    await p.fill('#d_not', '2026-06-20');
    await p.click('[data-v="si"][onclick*="residente"]');
    await p.click('[data-v="no"][onclick*="noleggio"]');
    await p.selectOption('#tipo_accertamento', 'immediata');
    await p.click('[data-v="si"][onclick*="firma"]');
    await p.click('[data-v="no"][onclick*="dati"]');
    await p.fill('#dati_errore_desc', 'targa AB123CD invece di AB123CE');
    await p.click('button:has-text("Genera la diagnosi")');
    await p.waitForTimeout(150);
    const testo = await p.locator('#risultato').innerText();
    check('Caso5: il dettaglio errore inserito appare nel motivo', testo.includes('AB123CD invece di AB123CE'));
    const ricorso = await p.locator('#testoRicorso').innerText();
    check('Caso5: il ricorso include il motivo con la descrizione errore', ricorso.includes('vizio di forma'));
    await p.close();
  }

  // --- CASO 6: termini di ricorso, notifica molto vecchia -> deve indicare "scaduti" ---
  {
    const p = await page();
    await p.fill('#d_inf', '2020-01-01');
    await p.fill('#d_not', '2020-01-20');
    await p.click('[data-v="si"][onclick*="residente"]');
    await p.click('[data-v="no"][onclick*="noleggio"]');
    await p.selectOption('#tipo_accertamento', 'immediata');
    await p.click('[data-v="si"][onclick*="firma"]');
    await p.click('[data-v="si"][onclick*="dati"]');
    await p.click('button:has-text("Genera la diagnosi")');
    await p.waitForTimeout(150);
    const testo = await p.locator('#risultato').innerText();
    check('Caso6: termini scaduti rilevati correttamente', testo.includes('scaduti') || testo.includes('non più praticabile'), testo.slice(0,200));
    await p.close();
  }

  // --- CASO 7: stampa PDF senza errori ---
  {
    const p = await page();
    await p.fill('#d_inf', '2026-06-01');
    await p.fill('#d_not', '2026-06-20');
    await p.click('[data-v="si"][onclick*="residente"]');
    await p.click('[data-v="no"][onclick*="noleggio"]');
    await p.selectOption('#tipo_accertamento', 'immediata');
    await p.click('[data-v="si"][onclick*="firma"]');
    await p.click('[data-v="si"][onclick*="dati"]');
    await p.click('button:has-text("Genera la diagnosi")');
    await p.waitForTimeout(150);
    await p.pdf({ path: 'multa-stampa.pdf', format: 'A4' }).then(()=>check('Caso7: generazione PDF', true)).catch(e=>check('Caso7: generazione PDF', false, e.message));
    await p.screenshot({ path: 'multa-screenshot.png', fullPage: true });
    check('Caso7: nessun errore JS accumulato', p._errs.length === 0, p._errs.join('|'));
    await p.close();
  }

  console.log('\n' + '='.repeat(70));
  console.log('RISULTATO: ' + PASS + ' superati, ' + FAIL + ' falliti su ' + (PASS+FAIL));
  await b.close();
  if (FAIL > 0) process.exit(1);
})();
