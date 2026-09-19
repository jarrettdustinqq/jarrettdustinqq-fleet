'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { chromium } = require('/home/jarrettdustinqq/agent_workspace/live-browser-mcp-20260918/node_modules/playwright');
const [mode, endpoint, out] = process.argv.slice(2);
const target = 'https://jarrettdustinqq.github.io/discovery-workbench/';
const file = path.join(out, `client-${mode}.json`);
let navigation;
function write(data) {
  fs.writeFileSync(file + '.tmp', JSON.stringify(data, null, 2) + '\n', {mode: 0o600});
  fs.renameSync(file + '.tmp', file);
}
(async () => {
  const browser = await chromium.connectOverCDP(endpoint, {timeout: 15000});
  const context = browser.contexts()[0];
  if (!context) throw new Error('default context missing');
  const prior = mode === 'b' ? JSON.parse(fs.readFileSync(path.join(out, 'client-a.json'))) : null;
  let page, targetId;
  for (const candidate of context.pages()) {
    const session = await context.newCDPSession(candidate);
    const info = await session.send('Target.getTargetInfo');
    await session.detach();
    if (!prior || info.targetInfo.targetId === prior.target) {
      page = candidate; targetId = info.targetInfo.targetId; break;
    }
  }
  if (!page) throw new Error('original default page/target missing');
  if (mode === 'a') {
    const started = performance.now();
    const response = await page.goto(target, {waitUntil: 'load', timeout: 15000});
    navigation = {status: 'failed', http_status: response ? response.status() : null,
      title: await page.title(), url: page.url(), elapsed_ms: performance.now() - started};
    if (navigation.http_status !== 200 || navigation.title !== 'Discovery Workbench' || navigation.url !== target)
      throw new Error('navigation requirements failed');
    navigation.status = 'passed';
    await page.evaluate(nonce => { window.__recoveryProbe = nonce; sessionStorage.setItem('__recoveryProbe', nonce); }, crypto.randomBytes(24).toString('hex'));
  }
  const observed = await page.evaluate(() => ({nonce: window.__recoveryProbe,
    session_nonce: sessionStorage.getItem('__recoveryProbe'), time_origin: performance.timeOrigin}));
  if (!observed.nonce || observed.nonce !== observed.session_nonce) throw new Error('nonce mismatch');
  if (prior && (observed.nonce !== prior.nonce || observed.session_nonce !== prior.session_nonce || observed.time_origin !== prior.time_origin))
    throw new Error('same-session recovery mismatch');
  await page.screenshot({path: path.join(out, mode === 'a' ? 'before.png' : 'after.png'), timeout: 15000});
  write({status: 'passed', endpoint, target: targetId, ...observed, ...(navigation ? {navigation} : {})});
  if (mode === 'a') setInterval(() => {}, 1000);
  else process.exit(0); // Drop this transport only; never issue browser/context/page.close().
})().catch(error => {
  write({status: 'failed', error: String(error), ...(navigation ? {navigation} : {})});
  process.exit(1);
});
