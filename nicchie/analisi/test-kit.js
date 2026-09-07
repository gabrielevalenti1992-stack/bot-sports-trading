const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' }).catch(()=>chromium.launch());
  const p = await b.newPage({ viewport: { width: 400, height: 860 } });
  const errs = [];
  p.on('pageerror', e => errs.push('PAGEERROR: ' + e.message));
  p.on('console', m => { if (m.type()==='error') errs.push('CONSOLE: ' + m.text()); });
  await p.goto('file:///home/user/bot-sports-trading/nicchie/kit-caparra/kit-caparra.html');
  await p.waitForTimeout(400);

  console.log('--- CODIFICA CARATTERI ---');
  console.log('titolo h1:', await p.locator('h1').innerText());
  console.log('sottotitolo:', (await p.locator('.sub').innerText()).slice(0,60));

  console.log('\n--- TEST 1: limite tre mensilita ---');
  await p.fill('#f_can','600'); await p.fill('#f_dep','2400'); await p.waitForTimeout(150);
  console.log((await p.locator('#alert_dep').innerText()).replace(/\s+/g,' ').slice(0,150));
  await p.fill('#f_dep','1200'); await p.waitForTimeout(150);
  console.log((await p.locator('#alert_dep').innerText()).replace(/\s+/g,' ').slice(0,90));

  console.log('\n--- TEST 2: interessi legali su piu anni ---');
  await p.click('[data-go="s4"]');
  await p.fill('#i_dep','1200'); await p.fill('#i_da','2023-01-01'); await p.fill('#i_a','2026-09-07');
  await p.waitForTimeout(250);
  console.log((await p.locator('#int_out').innerText()).replace(/\n+/g,' | '));

  console.log('\n--- TEST 3: verifica matematica indipendente ---');
  const atteso = 1200*0.05*(365/365) + 1200*0.025*(366/366) + 1200*0.02*(365/365) + 1200*0.016*(250/365);
  console.log('atteso (calcolo indipendente):', atteso.toFixed(2), '€');

  console.log('\n--- TEST 4: verbale stanze ---');
  await p.click('[data-go="s2"]');
  console.log('stanze generate:', await p.locator('.room').count());
  await p.locator('.rate[data-i="2"] button[data-v="danno"]').click();
  console.log('voto applicato:', await p.locator('.rate[data-i="2"] button[data-v="danno"]').getAttribute('class'));

  console.log('\n--- TEST 5: lettera di diffida ---');
  await p.click('[data-go="s5"]');
  await p.fill('#d_mot','tinteggiatura pareti'); await p.waitForTimeout(200);
  const L = await p.locator('#lettera').innerText();
  console.log('lunghezza lettera:', L.length, 'caratteri');
  ['8526/2020','1590 c.c.','392/1978','1219 c.c.','633'].forEach(k =>
    console.log('  contiene', k, ':', L.includes(k)));
  console.log('interessi riportati in lettera:', /interessi legali maturati, pari a euro [\d.]+/.test(L));

  console.log('\n--- TEST 6: persistenza ---');
  await p.reload(); await p.waitForTimeout(400);
  console.log('canone dopo reload:', await p.inputValue('#f_can'));
  console.log('motivazione dopo reload:', await p.inputValue('#d_mot'));

  console.log('\n--- TEST 7: foto e stampa ---');
  await p.click('[data-go="s3"]');
  console.log('scatti in protocollo:', await p.locator('#shots li').count());
  await p.pdf({ path: 'kit-stampa.pdf', format: 'A4' }).then(()=>console.log('PDF generato: ok'))
    .catch(e=>console.log('PDF: ' + e.message));

  await p.click('[data-go="s6"]');
  await p.screenshot({ path: 'kit-guida.png' });
  await p.click('[data-go="s4"]');
  await p.screenshot({ path: 'kit-interessi.png' });

  console.log('\n' + (errs.length ? 'ERRORI JS:\n'+errs.join('\n') : 'Nessun errore JS'));
  await b.close();
})();
